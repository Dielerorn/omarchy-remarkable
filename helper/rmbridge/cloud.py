"""reMarkable cloud access through rmapi (github.com/ddvk/rmapi).

rmapi does the sync protocol, so every write goes through a tool that already
handles the cloud's generation checks. This module only ever runs the
subcommands in ALLOWED: rmapi also has `nuke`, which empties the account.

Commands whose arguments name documents or folders (VIA_STDIN) are typed
into rmapi's shell on stdin instead of passed as arguments: every local
account can read the process list. That shell reads one command per line,
continues a line ending in a backslash, starts a heredoc at "<<", and treats
control characters as editing keys, so shell_line() only ever sends one
quoted line and refuses arguments that could change how it's split.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import unicodedata
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import config

RMAPI_VERSION = "v0.0.35"
RMAPI_URL = f"https://github.com/ddvk/rmapi/releases/download/{RMAPI_VERSION}/rmapi-linux-amd64.tar.gz"
RMAPI_SHA256 = "117616151d11937446ead6972b0934f97155443087f8406141db38fd1ac8fb25"
CONNECT_URL = "https://my.remarkable.com/device/browser/connect"

ALLOWED = {"ls", "find", "stat", "get", "geta", "put", "mkdir", "mv", "account", "refresh", "version"}
VIA_STDIN = {"ls", "stat", "geta", "put", "mkdir", "mv"}
SHELL_BANNER = "ReMarkable Cloud API Shell"
TRASH = "trash"


class CloudError(RuntimeError):
    pass


class NotPaired(CloudError):
    pass


def rmapi_path() -> Path:
    local = config.DATA_DIR / "bin" / "rmapi"
    if local.exists():
        return local
    found = shutil.which("rmapi")
    return Path(found) if found else local


def ensure_rmapi() -> Path:
    path = rmapi_path()
    if path.exists():
        return path
    target = config.DATA_DIR / "bin" / "rmapi"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=config.CACHE_DIR) as tmp:
        archive = Path(tmp) / "rmapi.tar.gz"
        urllib.request.urlretrieve(RMAPI_URL, archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != RMAPI_SHA256:
            raise CloudError(f"rmapi download failed its checksum ({digest})")
        with tarfile.open(archive) as tar:
            member = tar.getmember("rmapi")
            if not member.isfile():
                raise CloudError("unexpected rmapi archive layout")
            with tar.extractfile(member) as src, open(target, "wb") as dst:  # type: ignore[union-attr]
                shutil.copyfileobj(src, dst)
    target.chmod(0o755)
    return target


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["RMAPI_CONFIG"] = str(config.RMAPI_CONFIG)
    # Keep rmapi's tree cache apart from any rmapi the user runs by hand.
    env["XDG_CACHE_HOME"] = str(config.CACHE_DIR / "rmapi-cache")
    return env


@contextmanager
def _lock() -> Iterator[None]:
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.RUNTIME_DIR / "rmapi.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def is_paired() -> bool:
    try:
        return "devicetoken" in config.RMAPI_CONFIG.read_text()
    except OSError:
        return False


def run(args: list[str], *, cwd: Path | None = None, timeout: int = 300, json_out: bool = False) -> str:
    if not args or args[0] not in ALLOWED:
        raise CloudError(f"refusing to run rmapi {args[:1]}")
    if not is_paired():
        raise NotPaired("not connected to the reMarkable cloud")
    cmd = [str(ensure_rmapi()), "-ni"]
    if json_out:
        cmd.append("-json")
    line = None
    if args[0] in VIA_STDIN:
        line = shell_line(args)
    else:
        cmd += args
    with _lock():
        proc = subprocess.run(cmd, cwd=cwd, env=_env(), input=line, capture_output=True, text=True,
                              timeout=timeout)
    out = proc.stdout
    err = proc.stderr.strip()
    if line is not None:
        out, shell_error = shell_output(out)
        if shell_error:
            if "Enter one-time code" in out or "failed to get user token" in shell_error.lower():
                raise NotPaired("the cloud login has expired; connect again")
            raise CloudError(shell_error)
    if proc.returncode != 0 or "ERROR:" in err or out.lstrip().startswith("ERROR"):
        message = err or out.strip() or f"rmapi exited {proc.returncode}"
        if "Enter one-time code" in message or "failed to get user token" in message.lower():
            raise NotPaired("the cloud login has expired; connect again")
        raise CloudError(_clean(message))
    return out


# Line breaks, tab, NUL and readline's editing keys (Cc), the Unicode line and
# paragraph separators, and lone surrogates (undecodable bytes in file names).
FRAMING_CATEGORIES = ("Cc", "Cs", "Zl", "Zp")


def framing_unsafe(text: str) -> bool:
    """True if `text` could split or reshape a line in rmapi's shell."""
    return "<<" in text or any(unicodedata.category(c) in FRAMING_CATEGORIES for c in text)


def shell_line(args: list[str]) -> str:
    """Exactly one quoted command line for rmapi's shell, or CloudError.

    Every argument is checked, then the quoted line is parsed back (POSIX
    rules, the same as the google/shlex parser rmapi's shell uses) and must
    give back exactly `args`, so no other command can be selected.
    """
    if args[0] not in ALLOWED:
        raise CloudError(f"refusing to run rmapi {args[:1]}")
    if any(framing_unsafe(a) for a in args):
        raise CloudError("a name with a line break, control character or \"<<\" can't be passed to "
                         "rmapi safely; rename it first")
    line = shlex.join(args)
    if framing_unsafe(line) or shlex.split(line) != list(args) or line.rstrip().endswith("\\"):
        raise CloudError("couldn't quote that rmapi command safely")
    return line + "\n"


def shell_output(out: str) -> tuple[str, str]:
    """rmapi's shell wraps a command's output in a banner line and a final
    "EOF", and reports failures as "Error: ..." lines with exit code 0."""
    lines = out.splitlines()
    if lines and lines[0].startswith(SHELL_BANNER):
        lines = lines[1:]
    if lines and lines[-1].strip() == "EOF":
        lines = lines[:-1]
    errors = [l.split("Error:", 1)[1].strip() for l in lines if l.lstrip().startswith("Error:")]
    return "\n".join(lines) + ("\n" if lines else ""), (errors[-1] if errors else "")


def _clean(message: str) -> str:
    lines = [l for l in message.splitlines() if l.strip()]
    last = lines[-1] if lines else message
    # "ERROR: 2026/10/26 00:12:49 main.go:86: Error:  Failed to ..." -> "Failed to ..."
    if "Error:" in last:
        last = last.split("Error:", 1)[-1]
    elif ".go:" in last:
        last = last.split(".go:", 1)[-1].split(":", 1)[-1]
    return last.strip()


def pair(code: str) -> None:
    code = code.strip()
    if len(code) != 8 or not code.isalpha():
        raise CloudError("the one-time code is 8 letters")
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _lock():
        proc = subprocess.run(
            [str(ensure_rmapi()), "account"], input=code + "\n", env=_env(),
            capture_output=True, text=True, timeout=120,
        )
    if not is_paired():
        raise CloudError(_clean(proc.stderr or proc.stdout or "pairing failed"))
    os.chmod(config.RMAPI_CONFIG, 0o600)


def unpair() -> None:
    try:
        config.RMAPI_CONFIG.unlink()
    except FileNotFoundError:
        pass


@dataclass
class Entry:
    id: str
    name: str
    is_folder: bool
    parent: str
    modified: str
    current_page: int = 0
    starred: bool = False
    tags: list[str] = field(default_factory=list)
    path: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id, "name": self.name, "folder": self.is_folder, "parent": self.parent,
            "modified": self.modified, "currentPage": self.current_page, "starred": self.starred,
            "tags": self.tags, "path": self.path,
        }


class Library:
    def __init__(self, entries: list[Entry]):
        self.entries = entries
        self.by_id = {e.id: e for e in entries}
        for e in entries:
            e.path = self._path(e)

    def _path(self, e: Entry) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        node: Entry | None = e
        while node is not None and node.id not in ("", TRASH) and node.id not in seen:
            seen.add(node.id)
            parts.append(node.name)
            node = self.by_id.get(node.parent)
        if node is not None and node.id == TRASH:
            parts.append(TRASH)
        return "/" + "/".join(reversed(parts))

    def documents(self, include_trash: bool = False) -> list[Entry]:
        return [e for e in self.entries if not e.is_folder and e.id and (include_trash or not self.in_trash(e))]

    def folders(self) -> list[Entry]:
        return [e for e in self.entries if e.is_folder and e.id not in ("", TRASH) and not self.in_trash(e)]

    def in_trash(self, e: Entry) -> bool:
        seen: set[str] = set()
        node: Entry | None = e
        while node is not None and node.id not in seen:
            if node.parent == TRASH or node.id == TRASH:
                return True
            seen.add(node.id)
            node = self.by_id.get(node.parent)
        return False

    def find_path(self, path: str) -> Entry | None:
        path = "/" + path.strip("/")
        for e in self.entries:
            if e.path == path and e.id:
                return e
        return None

    def children(self, parent_id: str) -> list[Entry]:
        return [e for e in self.entries if e.parent == parent_id and e.id not in ("", TRASH)]

    def to_json(self) -> list[dict]:
        return [e.to_json() for e in self.entries if e.id not in ("",)]


def library() -> Library:
    raw = json.loads(run(["find", "/"], json_out=True, timeout=180) or "[]")
    entries = [
        Entry(
            id=x.get("id", ""), name=x.get("name", ""), is_folder=x.get("type") == "CollectionType",
            parent=x.get("parent", ""), modified=x.get("modifiedClient", ""),
            current_page=int(x.get("currentPage") or 0), starred=bool(x.get("starred")),
            tags=list(x.get("tags") or []),
        )
        for x in raw
    ]
    return Library(entries)


def download(entry: Entry) -> Path:
    """Fetch a document's .rmdoc, cached by id and modification time."""
    key = hashlib.sha1(entry.modified.encode()).hexdigest()[:12]
    target = config.DOC_CACHE / entry.id / f"{key}.rmdoc"
    if target.exists() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as tmp:
        run(["get", "--id", entry.id], cwd=Path(tmp), timeout=600)
        got = [p for p in Path(tmp).iterdir() if p.suffix in (".rmdoc", ".zip")]
        if not got:
            raise CloudError(f"download of {entry.name} produced no file")
        shutil.move(str(got[0]), target)
    for old in target.parent.glob("*.rmdoc"):
        if old != target:
            old.unlink(missing_ok=True)
    return target


def _remote_dir(folder: str) -> str:
    return "/" + folder.strip("/") if folder.strip("/") else "/"


def upload(local: Path, folder: str = "", *, name: str | None = None, cover_first_page: bool = False) -> Entry | None:
    """Upload a PDF/EPUB (or other supported file) and return its new entry."""
    if not local.exists():
        raise CloudError(f"{local} does not exist")
    src = local
    tmpdir = None
    stem = safe_filename(name or local.stem)
    if stem != local.stem or framing_unsafe(str(local)):
        # rmapi names the document after the file, so stage a copy with the
        # wanted (and shell-safe) name; the original path may not be safe.
        tmpdir = tempfile.mkdtemp(dir=config.TMP_DIR)
        suffix = "".join(c for c in local.suffix.lower() if c.isalnum() or c == ".")
        src = Path(tmpdir) / (stem + suffix)
        shutil.copyfile(local, src)
    try:
        remote = _remote_dir(folder)
        if remote != "/":
            ensure_folder(folder)
        args = ["put"]
        if cover_first_page:
            args.append("--coverpage=1")
        args += [str(src), remote]
        run(args, timeout=900)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    lib = library()
    wanted = (remote.rstrip("/") + "/" + src.stem) if remote != "/" else "/" + src.stem
    new = lib.find_path(wanted)
    if new:
        mark_self_write(new.id)
    return new


def safe_filename(name: str) -> str:
    """A document or folder name rmapi can take: no path separators, no
    line breaks or control characters, no "<<" (see shell_line)."""
    keep = "".join("-" if c in "/\\" else " " if unicodedata.category(c) in FRAMING_CATEGORIES else c
                   for c in name)
    while "<<" in keep:
        keep = keep.replace("<<", "<")
    return keep.strip()[:150] or "Untitled"


def ensure_folder(folder: str) -> None:
    parts = [p for p in folder.strip("/").split("/") if p]
    if not parts:
        return
    lib = library()
    path = ""
    for part in parts:
        path += "/" + part
        if lib.find_path(path) is None:
            run(["mkdir", path])
            lib = library()


def mkdir(path: str) -> Entry | None:
    ensure_folder(path)
    return library().find_path(path)


def move(entry: Entry, dest_folder: str, new_name: str | None = None) -> None:
    """Move and/or rename. dest_folder is a cloud path ("" = top level)."""
    target = _remote_dir(dest_folder)
    if new_name:
        target = (target.rstrip("/") + "/" + safe_filename(new_name)) if target != "/" else "/" + safe_filename(new_name)
    run(["mv", entry.path, target])
    mark_self_write(entry.id)


def rename(entry: Entry, new_name: str) -> None:
    parent = entry.path.rsplit("/", 1)[0]
    move(entry, parent, new_name)


def trash(entry: Entry) -> None:
    """Move to the tablet's own trash, where it can still be restored."""
    run(["mv", entry.path, "/" + TRASH])
    mark_self_write(entry.id)


def account() -> dict:
    out = run(["account"], timeout=60)
    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


SELF_WRITES = config.RUNTIME_DIR / "self-writes.json"


def mark_self_write(*ids: str) -> None:
    """Remember documents this plugin changed, so the daemon doesn't announce them."""
    now = time.time()
    try:
        marks = json.loads(SELF_WRITES.read_text())
    except (OSError, ValueError):
        marks = {}
    marks = {k: v for k, v in marks.items() if now - v < 900}
    for i in ids:
        if i:
            marks[i] = now
    config.write_json_atomic(SELF_WRITES, marks)


def self_writes(max_age: float = 900) -> set[str]:
    try:
        marks = json.loads(SELF_WRITES.read_text())
    except (OSError, ValueError):
        return set()
    now = time.time()
    return {k for k, v in marks.items() if now - v < max_age}


def wait_for(predicate, timeout: float = 30, interval: float = 2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None
