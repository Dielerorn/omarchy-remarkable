"""Two folders on this computer that follow the tablet.

- Mirror (settings mirrorDir): every document as an annotated PDF, laid out
  like the tablet's folders, refreshed when a document changes. One-way: it
  only ever deletes files it wrote itself.
- Send folder (settings sendDir): drop a file in and it's uploaded, then moved
  to Send/Sent. A subfolder becomes the cloud folder of the same name.
"""

from __future__ import annotations

import argparse
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import cloud, config, render
from .library import fetch_library, open_document, safe_name

SENT = "Sent"
SETTLE_SECONDS = 4  # a file still being written by a browser/download isn't touched


def _ok(**kw):
    return {"ok": True, **kw}


def mirror_target(root: Path, entry: cloud.Entry) -> Path:
    parts = [safe_name(p) for p in entry.path.strip("/").split("/")]
    return root.joinpath(*parts[:-1], parts[-1] + ".pdf")


def run_mirror(lib: cloud.Library | None = None, *, full: bool = False, on_progress=None) -> dict:
    settings = config.load_settings()
    root = config.setting_path(settings, "mirrorDir")
    root.mkdir(parents=True, exist_ok=True)
    state = config.load_state()
    done: dict = state.get("mirror", {})  # id -> {"modified", "file"}
    lib = lib or fetch_library()
    docs = lib.documents()
    written, kept, failed = [], 0, []
    alive = set()
    for n, e in enumerate(docs, 1):
        target = mirror_target(root, e)
        alive.add(e.id)
        prev = done.get(e.id)
        if not full and prev and prev.get("modified") == e.modified and Path(prev.get("file", "")).exists() \
                and prev.get("file") == str(target):
            kept += 1
            continue
        try:
            _, d = open_document(e.id)
            render.export_pdf(d, target)
            if prev and prev.get("file") and prev["file"] != str(target):
                _remove_owned(Path(prev["file"]), root)
            done[e.id] = {"modified": e.modified, "file": str(target)}
            written.append(str(target))
        except Exception as ex:  # one bad document shouldn't stop the rest
            failed.append({"name": e.name, "error": str(ex)})
        if on_progress:
            on_progress(n, len(docs))
        # Save as we go so an interrupted run resumes instead of restarting.
        if len(written) % 10 == 0:
            state["mirror"] = done
            config.save_state(state)
    removed = []
    for doc_id in [i for i in done if i not in alive]:
        f = Path(done.pop(doc_id).get("file", ""))
        if _remove_owned(f, root):
            removed.append(str(f))
    state = config.load_state()
    state["mirror"] = done
    state["mirrorLast"] = time.time()
    config.save_state(state)
    return {"written": written, "unchanged": kept, "removed": removed, "failed": failed, "dir": str(root)}


def _remove_owned(f: Path, root: Path) -> bool:
    """Delete a mirror file we wrote, and tidy empty folders up to the root."""
    try:
        f.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    if not f.exists():
        return False
    f.unlink()
    parent = f.parent
    while parent != root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
    return True


def pending_sends(settings: dict) -> list[tuple[Path, str]]:
    """(file, cloud folder) for every settled file waiting in the send folder."""
    from .send import DIRECT, IMAGES, TEXT
    root = config.setting_path(settings, "sendDir")
    if not root.is_dir():
        return []
    now = time.time()
    out = []
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root)
        if not f.is_file() or rel.parts[0] == SENT or any(p.startswith(".") for p in rel.parts):
            continue
        if f.suffix.lower() not in DIRECT | IMAGES | TEXT or f.name.endswith((".part", ".crdownload")):
            continue
        if now - f.stat().st_mtime < SETTLE_SECONDS:
            continue
        folder = "/".join(rel.parts[:-1]) or settings["uploadFolder"]
        out.append((f, folder))
    return out


def scan_send_folder(settings: dict | None = None) -> dict:
    from .send import send_file
    settings = settings or config.load_settings()
    root = config.setting_path(settings, "sendDir")
    root.mkdir(parents=True, exist_ok=True)
    sent, errors = [], []
    for f, folder in pending_sends(settings):
        try:
            result = send_file(f, folder)
            dest = root / SENT / datetime.now().strftime("%Y-%m-%d") / f.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest.with_name(f"{dest.stem} ({int(time.time())}){dest.suffix}")
            shutil.move(str(f), dest)
            sent.append(result)
        except Exception as ex:
            errors.append({"file": str(f), "error": str(ex)})
    return {"sent": sent, "errors": errors, "dir": str(root)}


def cmd_mirror(args: argparse.Namespace) -> dict:
    from .cli import progress
    result = run_mirror(full=args.full, on_progress=lambda n, t: progress("mirroring", n, t))
    return _ok(**result)


def cmd_sendfolder(args: argparse.Namespace) -> dict:
    result = scan_send_folder()
    return {"ok": not result["errors"], **result}


def register(sub) -> None:
    p = sub.add_parser("mirror", help="keep annotated PDFs of every document in a local folder")
    p.add_argument("--full", action="store_true", help="re-export everything")
    p.set_defaults(func=cmd_mirror)
    s = sub.add_parser("sendfolder", help="upload whatever is waiting in the send folder")
    s.set_defaults(func=cmd_sendfolder)
