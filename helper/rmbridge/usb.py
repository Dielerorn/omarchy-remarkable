"""The tablet's USB web interface (http://10.11.99.1, no developer mode needed).

Turned on under Settings → Storage → USB web interface; it only answers while
the tablet is plugged in by USB-C and awake. Routes (remarkable.guide
tech/usb-web-interface, and laithalsaadoon/remarkable-spec's measured
http.json for Paper Pro firmware 3.27):

  GET /documents/            root listing (trailing slash required)
  GET /documents/{folderId}  a folder's listing; any id that isn't a folder
                             silently returns the ROOT listing, so check Parent
  GET /download/{id}/pdf     device-rendered PDF
  GET /download/{id}/rmdoc   the document's own files as a zip (firmware 3.9+)
  POST /upload               multipart, one part named "file"; goes to the
                             folder listed last (the web UI lists the target
                             folder right before uploading) → 201
  GET /log.txt               the system journal

Listing entries carry ID, VisibleName and its misspelled twin VissibleName,
Type (DocumentType | CollectionType), ModifiedClient, Parent, fileType,
CurrentPage, Bookmarked; older firmware also sizeInBytes (a string) and
pageCount. JSON is labelled ISO-8859-1 but is UTF-8. Trashed documents never
appear. The server ignores the HTTP method and a HEAD leaves an unread body
on the connection, so every request here is a GET/POST with Connection: close.
"""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
import re
import selectors
import socket
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import config
from .tablet_remote import usb_link_up

HOST = "10.11.99.1"
HTTP_PORT = 80
SSH_PORT = 22
ENABLE_HINT = "Turn on Settings → Storage → USB web interface on the tablet."
CONNECT_HINT = "Plug the tablet in by USB-C and wake it up."
UPLOAD_TYPES = {".pdf": "application/pdf", ".epub": "application/epub+zip", ".rmdoc": "application/zip"}

Progress = Callable[..., None]


class UsbError(RuntimeError):
    code = "usb"


def _target() -> tuple[str, int]:
    """HOST:HTTP_PORT, overridable with OMARCHY_REMARKABLE_USB_HOST=host[:port] for testing."""
    override = os.environ.get("OMARCHY_REMARKABLE_USB_HOST")
    if override:
        host, _, port = override.partition(":")
        return host, int(port or 80)
    return HOST, HTTP_PORT


# ================================================================ detect


def detect(timeout: float = 1.0) -> dict:
    """Is the tablet on the USB link, and is the web interface on?

    Both ports are tried at once and the whole check, HTTP included, stays
    inside `timeout`.
    """
    deadline = time.monotonic() + timeout
    host, http_port = _target()
    ports = [http_port] + ([SSH_PORT] if not os.environ.get("OMARCHY_REMARKABLE_USB_HOST") else [])
    result = {"connected": False, "webInterface": False, "host": host}
    if host == HOST and not usb_link_up(host):
        return result  # not plugged in: answer at once instead of waiting out the timeout
    sel = selectors.DefaultSelector()
    socks: dict[socket.socket, int] = {}
    open_http: socket.socket | None = None
    try:
        for port in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError:
                continue
            socks[s] = port
            s.setblocking(False)
            # An immediate failure (no route) must not be mistaken for a connection later.
            if s.connect_ex((host, port)) not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK):
                continue
            sel.register(s, selectors.EVENT_WRITE)
        pending = {k.fileobj for k in sel.get_map().values()}
        while pending and open_http is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for key, _ in sel.select(remaining):
                s = key.fileobj  # type: ignore[assignment]
                pending.discard(s)  # type: ignore[arg-type]
                sel.unregister(s)
                if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:  # type: ignore[union-attr]
                    result["connected"] = True
                    if socks[s] == http_port:  # type: ignore[index]
                        open_http = s  # type: ignore[assignment]
            if result["connected"] and open_http is None and http_port not in [socks[p] for p in pending]:
                break
        if open_http is not None:
            result["webInterface"] = _quick_listing(open_http, host, deadline)
    finally:
        sel.close()
        for s in socks:
            s.close()
    return result


def _quick_listing(sock: socket.socket, host: str, deadline: float) -> bool:
    """GET /documents/ on an already-connected socket, giving up at the deadline."""
    try:
        sock.setblocking(True)
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        sock.sendall(f"GET /documents/ HTTP/1.1\r\nHost: {host}\r\nAccept: application/json\r\n"
                     "Connection: close\r\n\r\n".encode())
        data = b""
        while b"\r\n\r\n" not in data or len(data.split(b"\r\n\r\n", 1)[1]) < 1:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except OSError:
        return False
    head, _, body = data.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0]
    if b" 200 " not in status + b" ":
        return False
    if b"chunked" in head.lower():
        body = body.split(b"\r\n", 1)[-1]  # skip the first chunk-size line
    return body.lstrip()[:1] == b"["


# ================================================================ HTTP


def _request(method: str, path: str, *, body: bytes | None = None, headers: dict | None = None,
             timeout: float = 30, stream_to: Path | None = None, on_bytes=None) -> tuple[int, dict, bytes]:
    host, port = _target()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    hdrs = {"Connection": "close", "Accept": "*/*", "Origin": f"http://{host}", "Referer": f"http://{host}/"}
    hdrs.update(headers or {})
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        headers_out = {k.lower(): v for k, v in resp.getheaders()}
        if stream_to is not None and resp.status == 200:
            done = 0
            with open(stream_to, "wb") as out:
                while chunk := resp.read(1 << 16):
                    out.write(chunk)
                    done += len(chunk)
                    if on_bytes:
                        on_bytes(done)
            return resp.status, headers_out, b""
        return resp.status, headers_out, resp.read()
    except (OSError, http.client.HTTPException) as e:
        raise UsbError(f"The tablet's USB web interface isn't answering ({e}). {CONNECT_HINT} {ENABLE_HINT}") from e
    finally:
        conn.close()


def _error_text(body: bytes) -> str:
    try:
        return json.loads(body.decode("utf-8")).get("error") or ""
    except (ValueError, AttributeError, UnicodeDecodeError):
        return body.decode("utf-8", "replace").strip()[:200]


def _field(entry: dict, *names: str, default: Any = None) -> Any:
    for n in names:
        if n in entry and entry[n] is not None:
            return entry[n]
    return default


def normalize(entry: dict) -> dict:
    """One listing entry with stable field names, whichever spelling the firmware used."""
    kind = str(_field(entry, "Type", "type", default="DocumentType"))
    folder = kind == "CollectionType"
    size = _field(entry, "sizeInBytes", "SizeInBytes")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    pages = _field(entry, "pageCount", "PageCount")
    return {
        "id": str(_field(entry, "ID", "id", "Id", default="")),
        "name": str(_field(entry, "VisibleName", "VissibleName", "visibleName", "Name", default="")),
        "folder": folder,
        "parent": str(_field(entry, "Parent", "parent", default="") or ""),
        "modified": str(_field(entry, "ModifiedClient", "modifiedClient", "LastModified", "lastModified", default="")),
        "fileType": None if folder else _field(entry, "fileType", "FileType"),
        "currentPage": _field(entry, "CurrentPage", "currentPage"),
        "bookmarked": bool(_field(entry, "Bookmarked", "bookmarked", default=False)),
        "sizeInBytes": size,
        "pageCount": int(pages) if isinstance(pages, (int, float)) else None,
    }


def list_folder(folder: str = "") -> list[dict]:
    """Entries directly inside a folder ("" = top level)."""
    path = f"/documents/{folder}" if folder else "/documents/"
    status, headers, body = _request("GET", path, timeout=30)
    if status != 200:
        raise UsbError(f"Listing failed on the tablet ({status} {_error_text(body)}). {ENABLE_HINT}")
    try:
        raw = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise UsbError(f"The tablet sent a listing that isn't JSON. {ENABLE_HINT}") from e
    if not isinstance(raw, list):
        raise UsbError("The tablet sent an unexpected listing.")
    entries = [normalize(e) for e in raw if isinstance(e, dict)]
    if folder and entries and any(e["parent"] != folder for e in entries):
        # Unknown ids and document ids fall back to the root listing.
        raise UsbError(f"There's no folder with id {folder} on the tablet.")
    return entries


def walk(on_folder: Callable[[str, int], None] | None = None) -> list[dict]:
    """Every folder and document, each with a "path" ("/Work/Notes")."""
    out: list[dict] = []
    seen: set[str] = set()
    queue: list[tuple[str, str]] = [("", "")]
    while queue:
        folder, prefix = queue.pop(0)
        if folder in seen:
            continue
        seen.add(folder)
        try:
            entries = list_folder(folder)
        except UsbError:
            if not folder:
                raise
            continue
        if on_folder:
            on_folder(prefix or "/", len(entries))
        for e in entries:
            if e["id"] in seen:
                continue
            e["path"] = f"{prefix}/{e['name']}"
            out.append(e)
            if e["folder"]:
                queue.append((e["id"], e["path"]))
    return out


def _filename_from(headers: dict, fallback: str) -> str:
    disp = headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", disp, re.I)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', disp)
    if m:
        name = m.group(1)
        try:
            return name.encode("latin-1").decode("utf-8")  # http.client decodes headers as latin-1
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name
    return fallback


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", name).strip(" .")
    return name[:150] or "Untitled"


def download(doc_id: str, fmt: str, dest_dir: Path, *, filename: str | None = None,
             on_bytes=None, timeout: float = 600) -> Path:
    """Save a document as PDF or .rmdoc into dest_dir and return the file."""
    if fmt not in ("pdf", "rmdoc"):
        raise ValueError("format must be pdf or rmdoc")
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".download-")
    os.close(fd)
    try:
        status, headers, body = _request("GET", f"/download/{doc_id}/{fmt}", timeout=timeout,
                                         stream_to=Path(tmp), on_bytes=on_bytes)
        if status != 200:
            message = _error_text(body)
            if message == "No such entry":
                raise UsbError(f"There's no document with id {doc_id} on the tablet.")
            if message == "Can only download documents":
                raise UsbError("That id is a folder; only documents can be downloaded.")
            raise UsbError(f"The tablet couldn't export that document ({status} {message}).")
        name = filename or safe_name(_filename_from(headers, f"{doc_id}.{fmt}"))
        if not name.lower().endswith(f".{fmt}"):
            name += f".{fmt}"
        target = dest_dir / name
        os.replace(tmp, target)
        return target
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _multipart(field: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = f"----omarchy-remarkable-{uuid.uuid4().hex}"
    quoted = filename.replace('"', "'").replace("\r", " ").replace("\n", " ")
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{quoted}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n").encode()
    return head + data + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def upload(src: Path, folder: str = "", *, name: str | None = None) -> dict:
    """Upload a PDF, EPUB or .rmdoc (other files are converted to PDF first).

    The folder is listed right before the upload because that's how the
    tablet decides where the file goes; afterwards both the folder and the top
    level are checked so the result says where it really landed.
    """
    if not src.exists():
        raise ValueError(f"{src} doesn't exist")
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as tmp:
        ready = src
        if src.suffix.lower() not in UPLOAD_TYPES:
            from .send import prepare
            ready = prepare(src, Path(tmp))
        suffix = ready.suffix.lower()
        stem = name.strip() if name and name.strip() else src.stem
        filename = stem if stem.lower().endswith(suffix) else stem + suffix
        before_folder = {e["id"] for e in list_folder(folder)}
        before_root = before_folder if not folder else {e["id"] for e in list_folder("")}
        if folder:
            list_folder(folder)  # the listing just before the upload picks the destination
        body, ctype = _multipart("file", filename, UPLOAD_TYPES[suffix], ready.read_bytes())
        status, _, resp = _request("POST", "/upload", body=body, headers={"Content-Type": ctype}, timeout=600)
    if status not in (200, 201):
        raise UsbError(f"The tablet refused the upload ({status} {_error_text(resp)}).")
    new = None
    landed = folder
    for _ in range(10):
        new = next((e for e in list_folder(folder) if e["id"] not in before_folder), None)
        if new is None and folder:
            new = next((e for e in list_folder("") if e["id"] not in before_root), None)
            landed = "" if new else folder
        if new:
            break
        time.sleep(0.5)
    result = {"file": str(src), "uploadedAs": filename, "folder": folder, "landedIn": landed,
              "id": new["id"] if new else None, "name": new["name"] if new else None}
    if folder and new and landed != folder:
        result["warning"] = "The tablet put it at the top level instead of the chosen folder; move it on the tablet."
    return result


# ================================================================ backup


def _manifest_path(dest: Path) -> Path:
    return dest / "manifest.json"


def _load_manifest(dest: Path) -> dict:
    try:
        value = json.loads(_manifest_path(dest).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _assign_stems(documents: list[dict], known: dict) -> dict[str, str]:
    """Local path (without extension) per document id, stable across runs.

    Same-named documents in one folder get the id appended; a document keeps
    the name it already has on disk as long as that name still fits it, so
    renaming one document never shuffles another's files.
    """
    def plain(e: dict) -> str:
        parts = [safe_name(p) for p in e["path"].strip("/").split("/")[:-1]] + [safe_name(e["name"])]
        return "/".join(parts)

    stems: dict[str, str] = {}
    used: set[str] = set()
    for e in documents:  # first, keep valid existing names
        base = plain(e)
        files = (known.get(e["id"]) or {}).get("files") or {}
        old = next(iter(files.values()), "").rsplit(".", 1)[0]
        if old and old in (base, f"{base} ({e['id'][:8]})") and old.lower() not in used:
            stems[e["id"]] = old
            used.add(old.lower())
    for e in documents:
        if e["id"] in stems:
            continue
        base = plain(e)
        stem = base if base.lower() not in used else f"{base} ({e['id'][:8]})"
        stems[e["id"]] = stem
        used.add(stem.lower())
    return stems


def backup(dest: Path, on_progress: Progress | None = None, *, formats: tuple[str, ...] = ("rmdoc", "pdf")) -> dict:
    """Mirror every document into dest/<folder path>/ as .rmdoc and .pdf.

    Incremental: manifest.json remembers each document's ModifiedClient, so
    unchanged documents are skipped (and only moved if renamed or moved).
    Nothing local is deleted when a document disappears from the tablet.
    """
    dest.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(dest)
    docs = manifest.setdefault("documents", {})
    entries = walk()
    documents = [e for e in entries if not e["folder"]]
    stems = _assign_stems(documents, docs)
    counts = {"downloaded": 0, "skipped": 0, "moved": 0, "failed": 0}
    errors: list[dict] = []
    total = len(documents)
    for i, e in enumerate(documents, 1):
        stem = stems[e["id"]]
        known = docs.get(e["id"], {})
        wanted = {fmt: f"{stem}.{fmt}" for fmt in formats}
        if on_progress:
            on_progress(f"backing up {e['path']}", i, total)
        if known.get("modified") == e["modified"] and all((dest / f).exists() for f in known.get("files", {}).values()) \
                and set(known.get("files", {})) >= set(formats):
            if known.get("files") != wanted:
                for fmt, old in known["files"].items():
                    if fmt in wanted and old != wanted[fmt]:
                        (dest / wanted[fmt]).parent.mkdir(parents=True, exist_ok=True)
                        os.replace(dest / old, dest / wanted[fmt])
                known["files"] = {**known["files"], **wanted}
                known["path"] = e["path"]
                counts["moved"] += 1
            else:
                counts["skipped"] += 1
            continue
        files = {}
        try:
            for fmt in formats:
                target = dest / wanted[fmt]
                download(e["id"], fmt, target.parent, filename=target.name)
                files[fmt] = wanted[fmt]
        except (UsbError, OSError) as err:
            counts["failed"] += 1
            errors.append({"id": e["id"], "path": e["path"], "error": str(err)})
            continue
        for fmt, old in known.get("files", {}).items():
            if old != files.get(fmt) and (dest / old).exists():
                (dest / old).unlink()
        docs[e["id"]] = {"modified": e["modified"], "path": e["path"], "files": files, "fileType": e["fileType"]}
        counts["downloaded"] += 1
        manifest["updated"] = time.time()
        config.write_json_atomic(_manifest_path(dest), manifest)
    manifest["updated"] = time.time()
    config.write_json_atomic(_manifest_path(dest), manifest)
    return {"path": str(dest), "documents": total, "folders": len(entries) - total, **counts,
            "errors": errors[:20]}


# ================================================================ commands


def _guard(fn):
    def wrapper(args: argparse.Namespace) -> dict:
        try:
            return fn(args)
        except UsbError as e:
            return {"ok": False, "error": str(e), "code": e.code}
        except ValueError as e:
            return {"ok": False, "error": str(e), "code": "invalid"}
    return wrapper


@_guard
def cmd_status(args: argparse.Namespace) -> dict:
    state = detect(timeout=2.0)
    result = {"ok": True, **state}
    if not state["connected"]:
        result["hint"] = CONNECT_HINT
    elif not state["webInterface"]:
        result["hint"] = ENABLE_HINT
    else:
        result["topLevel"] = len(list_folder(""))
    return result


@_guard
def cmd_list(args: argparse.Namespace) -> dict:
    if args.recursive:
        entries = walk()
        if args.folder:
            root = next((e for e in entries if e["id"] == args.folder), None)
            if root is None:
                raise UsbError(f"There's no folder with id {args.folder} on the tablet.")
            entries = [e for e in entries if e["path"].startswith(root["path"] + "/")]
    else:
        entries = list_folder(args.folder or "")
    return {"ok": True, "folder": args.folder or "", "entries": entries}


@_guard
def cmd_download(args: argparse.Namespace) -> dict:
    from .cli import progress
    dest = Path(args.dest).expanduser() if args.dest else config.setting_path(config.load_settings(), "exportDir")
    last = [0]

    def on_bytes(n: int) -> None:
        if n - last[0] >= 1 << 20:
            last[0] = n
            progress(f"downloaded {n >> 20} MB")

    path = download(args.id, args.format, dest, on_bytes=on_bytes)
    return {"ok": True, "path": str(path), "format": args.format}


@_guard
def cmd_upload(args: argparse.Namespace) -> dict:
    return {"ok": True, **upload(Path(args.file).expanduser(), args.folder or "", name=args.name)}


@_guard
def cmd_backup(args: argparse.Namespace) -> dict:
    from .cli import progress
    dest = Path(args.dest).expanduser() if args.dest else config.setting_path(config.load_settings(), "backupDir") / "usb"
    formats = tuple(f for f in args.formats.split(",") if f in ("rmdoc", "pdf")) or ("rmdoc", "pdf")
    return {"ok": True, **backup(dest, on_progress=progress, formats=formats)}


def register(sub) -> None:
    p = sub.add_parser("usb", help="the tablet's USB web interface (no developer mode needed)")
    s = p.add_subparsers(dest="action", required=True)
    x = s.add_parser("status", help="is the tablet plugged in, is the web interface on")
    x.set_defaults(func=cmd_status)
    x = s.add_parser("list", help="list a folder")
    x.add_argument("--folder", help="folder id (default: top level)")
    x.add_argument("--recursive", action="store_true")
    x.set_defaults(func=cmd_list)
    x = s.add_parser("download", help="save a document as PDF or .rmdoc")
    x.add_argument("id")
    x.add_argument("--format", choices=["pdf", "rmdoc"], default="pdf")
    x.add_argument("--dest")
    x.set_defaults(func=cmd_download)
    x = s.add_parser("upload", help="upload a file (PDF, EPUB, .rmdoc; others converted to PDF)")
    x.add_argument("file")
    x.add_argument("--folder", help="folder id (default: top level)")
    x.add_argument("--name", help="file name to give the tablet")
    x.set_defaults(func=cmd_upload)
    x = s.add_parser("backup", help="incremental backup of everything as .rmdoc + .pdf")
    x.add_argument("--dest")
    x.add_argument("--formats", default="rmdoc,pdf")
    x.set_defaults(func=cmd_backup)
