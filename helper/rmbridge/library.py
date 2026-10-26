"""Library listing and per-document commands (view, export, organise)."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import cloud, config, render
from .doc import Document, load

LIBRARY_FILE = config.DATA_DIR / "library.json"
DOCMETA_FILE = config.DATA_DIR / "docmeta.json"


def _ok(**kw):
    return {"ok": True, **kw}


# ------------------------------------------------------------- library cache


def fetch_library() -> cloud.Library:
    lib = cloud.library()
    save_library_cache(lib)
    return lib


def save_library_cache(lib: cloud.Library) -> None:
    meta = load_docmeta()
    entries = []
    for e in lib.entries:
        if not e.id:
            continue
        j = e.to_json()
        j["trashed"] = lib.in_trash(e)
        m = meta.get(e.id)
        if m:
            j["fileType"] = m.get("fileType")
            j["pageCount"] = m.get("pages")
        entries.append(j)
    config.write_json_atomic(LIBRARY_FILE, {"fetched": time.time(), "entries": entries})


def cached_entries() -> list[dict]:
    try:
        return json.loads(LIBRARY_FILE.read_text()).get("entries", [])
    except (OSError, ValueError):
        return []


def entry(doc_id: str, *, refresh: bool = False) -> cloud.Entry:
    if not refresh:
        for j in cached_entries():
            if j["id"] == doc_id:
                return cloud.Entry(id=j["id"], name=j["name"], is_folder=j["folder"], parent=j["parent"],
                                   modified=j["modified"], current_page=j.get("currentPage", 0),
                                   starred=j.get("starred", False), tags=j.get("tags", []), path=j.get("path", ""))
    lib = fetch_library()
    e = lib.by_id.get(doc_id)
    if e is None:
        raise cloud.CloudError(f"no document with id {doc_id}")
    return e


def load_docmeta() -> dict:
    try:
        return json.loads(DOCMETA_FILE.read_text())
    except (OSError, ValueError):
        return {}


def remember(document: Document, modified: str) -> None:
    meta = load_docmeta()
    meta[document.id] = {"fileType": document.file_type, "pages": len(document.pages), "modified": modified,
                         "name": document.name}
    config.write_json_atomic(DOCMETA_FILE, meta)


def open_document(doc_id: str) -> tuple[cloud.Entry, Document]:
    e = entry(doc_id)
    try:
        path = cloud.download(e)
    except cloud.CloudError:
        e = entry(doc_id, refresh=True)  # cached entry may be stale
        path = cloud.download(e)
    document = load(path)
    if load_docmeta().get(doc_id, {}).get("modified") != e.modified:
        remember(document, e.modified)
    return e, document


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", name).strip(" .")
    return name[:150] or "Untitled"


# ------------------------------------------------------------------ commands


def cmd_library(args: argparse.Namespace) -> dict:
    if args.cached:
        entries = cached_entries()
        try:
            fetched = json.loads(LIBRARY_FILE.read_text()).get("fetched")
        except (OSError, ValueError):
            fetched = None
        return _ok(entries=entries, fetched=fetched, cached=True)
    fetch_library()
    return _ok(entries=cached_entries(), fetched=time.time(), cached=False)


def cmd_info(args: argparse.Namespace) -> dict:
    e, d = open_document(args.id)
    return _ok(id=d.id, name=d.name, fileType=d.file_type, pageCount=len(d.pages), path=e.path,
               modified=e.modified, currentPage=e.current_page, tags=d.tags,
               pages=[{"index": p.index, "annotated": p.annotated, "template": p.template, "pdfPage": p.pdf_page}
                      for p in d.pages])


def cmd_thumb(args: argparse.Namespace) -> dict:
    e, d = open_document(args.id)
    path = render.thumbnail(d, width=args.width)
    return _ok(id=d.id, path=str(path), fileType=d.file_type, pageCount=len(d.pages))


def cmd_page(args: argparse.Namespace) -> dict:
    e, d = open_document(args.id)
    if not 0 <= args.index < len(d.pages):
        return {"ok": False, "error": f"page {args.index + 1} of {len(d.pages)}"}
    path = render.page_png(d, args.index, width=args.width)
    return _ok(id=d.id, index=args.index, pageCount=len(d.pages), path=str(path))


def cmd_pages(args: argparse.Namespace) -> dict:
    from .cli import progress
    e, d = open_document(args.id)
    paths = []
    for i in range(len(d.pages)):
        paths.append(str(render.page_png(d, i, width=args.width)))
        progress("rendering", i + 1, len(d.pages))
    return _ok(id=d.id, paths=paths, pageCount=len(d.pages))


def export_document(doc_id: str, fmt: str, dest: Path | None = None, annotations: bool = True) -> list[Path]:
    e, d = open_document(doc_id)
    settings = config.load_settings()
    folder = dest or config.setting_path(settings, "exportDir")
    folder.mkdir(parents=True, exist_ok=True)
    base = safe_name(d.name)
    if fmt == "pdf":
        return [render.export_pdf(d, folder / f"{base}.pdf", annotations=annotations)]
    if fmt == "png":
        out = folder / base
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(len(d.pages)):
            src = render.page_png(d, i, width=1620)
            target = out / f"{base} - page {i + 1:03d}.png"
            target.write_bytes(src.read_bytes())
            paths.append(target)
        return paths
    if fmt == "svg":
        out = folder / base
        return [render.export_svg(d, i, out / f"{base} - page {i + 1:03d}.svg") for i in range(len(d.pages))]
    if fmt == "original":
        if d.pdf:
            target = folder / f"{base}.pdf"
            target.write_bytes(d.pdf)
            return [target]
        if d.epub:
            target = folder / f"{base}.epub"
            target.write_bytes(d.epub)
            return [target]
        return [render.export_pdf(d, folder / f"{base}.pdf", annotations=False)]
    if fmt == "rmdoc":
        target = folder / f"{base}.rmdoc"
        target.write_bytes(cloud.download(e).read_bytes())
        return [target]
    raise ValueError(f"unknown format {fmt}")


def cmd_export(args: argparse.Namespace) -> dict:
    paths = export_document(args.id, args.format, Path(args.dest).expanduser() if args.dest else None,
                            annotations=not args.no_annotations)
    if args.open and paths:
        from .cli import open_path
        open_path(paths[0] if args.format in ("pdf", "original", "rmdoc") else paths[0].parent)
    return _ok(paths=[str(p) for p in paths])


def cmd_open(args: argparse.Namespace) -> dict:
    """Open an annotated copy in the desktop PDF viewer."""
    from .cli import open_path
    e, d = open_document(args.id)
    target = config.CACHE_DIR / "open" / f"{safe_name(d.name)}.pdf"
    render.export_pdf(d, target)
    open_path(target)
    return _ok(path=str(target))


def cmd_rename(args: argparse.Namespace) -> dict:
    e = entry(args.id, refresh=True)
    cloud.rename(e, args.name)
    fetch_library()
    return _ok(id=args.id, name=args.name)


def cmd_move(args: argparse.Namespace) -> dict:
    e = entry(args.id, refresh=True)
    cloud.move(e, args.folder)
    fetch_library()
    return _ok(id=args.id, folder=args.folder)


def cmd_trash(args: argparse.Namespace) -> dict:
    e = entry(args.id, refresh=True)
    cloud.trash(e)
    fetch_library()
    return _ok(id=args.id, trashed=True)


def cmd_mkdir(args: argparse.Namespace) -> dict:
    created = cloud.mkdir(args.path)
    fetch_library()
    return _ok(id=created.id if created else None, path=args.path)


def iso_to_epoch(value: str) -> float:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


def register(sub) -> None:
    p = sub.add_parser("library", help="list every document and folder")
    p.add_argument("--cached", action="store_true", help="answer from the last fetch without going online")
    p.set_defaults(func=cmd_library)

    d = sub.add_parser("doc", help="view, export and organise one document")
    dsub = d.add_subparsers(dest="action", required=True)
    x = dsub.add_parser("info")
    x.add_argument("id")
    x.set_defaults(func=cmd_info)
    x = dsub.add_parser("thumb")
    x.add_argument("id")
    x.add_argument("--width", type=int, default=300)
    x.set_defaults(func=cmd_thumb)
    x = dsub.add_parser("page")
    x.add_argument("id")
    x.add_argument("index", type=int, help="0-based page index")
    x.add_argument("--width", type=int, default=1240)
    x.set_defaults(func=cmd_page)
    x = dsub.add_parser("pages")
    x.add_argument("id")
    x.add_argument("--width", type=int, default=1240)
    x.set_defaults(func=cmd_pages)
    x = dsub.add_parser("export")
    x.add_argument("id")
    x.add_argument("--format", choices=["pdf", "png", "svg", "original", "rmdoc"], default="pdf")
    x.add_argument("--dest")
    x.add_argument("--no-annotations", action="store_true")
    x.add_argument("--open", action="store_true", help="open the result afterwards")
    x.set_defaults(func=cmd_export)
    x = dsub.add_parser("open")
    x.add_argument("id")
    x.set_defaults(func=cmd_open)
    x = dsub.add_parser("rename")
    x.add_argument("id")
    x.add_argument("name")
    x.set_defaults(func=cmd_rename)
    x = dsub.add_parser("move")
    x.add_argument("id")
    x.add_argument("folder", help='cloud folder path, "" for the top level')
    x.set_defaults(func=cmd_move)
    x = dsub.add_parser("trash", help="move to the tablet's trash (restorable there)")
    x.add_argument("id")
    x.set_defaults(func=cmd_trash)

    f = sub.add_parser("folder", help="create folders")
    fsub = f.add_subparsers(dest="action", required=True)
    x = fsub.add_parser("create")
    x.add_argument("path")
    x.set_defaults(func=cmd_mkdir)
