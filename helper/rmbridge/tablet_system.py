"""Changes to the tablet's read-only system partition (templates, sleep screen).

The root filesystem is ext4 mounted read-only; `mount -o remount,rw /` lasts
until the next reboot, and a firmware update replaces the whole partition,
which silently undoes everything done here. Before the first change on a
given firmware the stock files are copied to
/home/root/.omarchy-remarkable-backup/<firmware>/... on the tablet (/home
survives updates) and to DATA_DIR/tablet-backup/<firmware>/... locally, so
"restore" always has the stock version to put back.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .tablet_remote import REMOTE_BACKUP, REMOTE_WORK, Remote, TabletError, q

SCREEN_W, SCREEN_H = 1620, 2160  # Paper Pro, portrait
SUSPENDED = "/usr/share/remarkable/suspended.png"
CAROUSEL_DIR = "/usr/share/remarkable/carousel"
FIRMWARE_WARNING = ("A firmware update replaces the tablet's system files and undoes this; "
                    "run it again after an update.")


@dataclass
class Change:
    target: str  # absolute path on the root filesystem
    data: bytes | None  # new contents; None deletes the file


def firmware(remote: Remote) -> str:
    """User-facing firmware version (IMG_VERSION, e.g. 3.27.3.0)."""
    out = remote.run("cat /etc/os-release 2>/dev/null; echo @@; "
                     "cat /usr/share/remarkable/update.conf 2>/dev/null; echo @@; "
                     "cat /etc/version 2>/dev/null", check=False).text
    return parse_firmware(out)


def parse_firmware(out: str) -> str:
    os_release, _, rest = out.partition("@@")
    update_conf, _, version = rest.partition("@@")
    for line in os_release.splitlines():
        if line.startswith("IMG_VERSION="):
            return line.split("=", 1)[1].strip().strip('"')
    for line in update_conf.splitlines():
        if line.startswith("REMARKABLE_RELEASE_VERSION="):
            return line.split("=", 1)[1].strip().strip('"')
    # /etc/version is only a build stamp, but it still separates firmwares.
    return version.strip() or "unknown"


def backup_root(fw: str) -> str:
    return f"{REMOTE_BACKUP}/{_safe_fw(fw)}"


def local_backup_root(fw: str) -> Path:
    return config.DATA_DIR / "tablet-backup" / _safe_fw(fw)


def _safe_fw(fw: str) -> str:
    return "".join(c if c.isalnum() or c in ".-_" else "_" for c in fw) or "unknown"


def backup_originals(remote: Remote, fw: str, paths: list[str]) -> list[str]:
    """Keep the first-seen (stock) copy of each path, on the tablet and here.

    Existing backups are never overwritten: the point is to remember what the
    firmware shipped, not what we last installed.
    """
    root = backup_root(fw)
    lines = []
    for p in paths:
        dest = f"{root}{p}"
        lines.append(f"if [ -e {q(p)} ] && [ ! -e {q(dest)} ]; then mkdir -p {q(dest.rsplit('/', 1)[0])} && "
                     f"cp -p {q(p)} {q(dest)} && echo saved:{q(p)}; fi")
    out = remote.run("\n".join(lines) or "true", timeout=60).text
    saved = [l.split(":", 1)[1] for l in out.splitlines() if l.startswith("saved:")]
    local = local_backup_root(fw)
    for p in paths:
        target = local / p.lstrip("/")
        if target.exists():
            continue
        if remote.exists(f"{root}{p}"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(remote.get(f"{root}{p}"))
    return saved


def original(remote: Remote | None, fw: str, path: str) -> bytes | None:
    """The stock copy of a file, from the local backup or the tablet's."""
    local = local_backup_root(fw) / path.lstrip("/")
    if local.exists():
        return local.read_bytes()
    if remote is not None and remote.exists(f"{backup_root(fw)}{path}"):
        return remote.get(f"{backup_root(fw)}{path}")
    return None


def apply(remote: Remote, changes: list[Change], *, restart: bool) -> dict:
    """Write files onto the root filesystem in one remount window.

    New contents are staged under /home first (checksummed), so the window
    where / is writable only covers local copies on the tablet.
    """
    staging = f"{REMOTE_WORK}/staging"
    remote.run(f"rm -rf {q(staging)} && mkdir -p {q(staging)}")
    script = [
        "set -e",
        "state=$(awk '$2==\"/\" {s=$4} END {print s}' /proc/mounts | cut -d, -f1)",
        "restore() { sync; if [ \"$state\" = ro ]; then mount -o remount,ro / || echo remount-ro-failed; fi; }",
        "trap restore EXIT",
        "if [ \"$state\" = ro ]; then mount -o remount,rw /; fi",
    ]
    for i, change in enumerate(changes):
        target = change.target
        if not target.startswith("/usr/share/remarkable/"):
            raise TabletError(f"refusing to change {target}")
        if change.data is None:
            script.append(f"rm -f {q(target)}")
            continue
        staged = f"{staging}/{i}"
        remote.put(change.data, staged)
        script += [f"mkdir -p {q(target.rsplit('/', 1)[0])}",
                   f"cp {q(staged)} {q(target)}.omr-new",
                   f"mv -f {q(target)}.omr-new {q(target)}"]
    script += ["restore", "trap - EXIT", f"rm -rf {q(staging)}"]
    if restart:
        script.append("systemctl restart xochitl")
    script.append("echo applied")
    out = remote.run("\n".join(script), timeout=120).text
    return {"applied": "applied" in out, "restarted": restart and "applied" in out,
            "rootReadOnly": "remount-ro-failed" not in out}


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def remote_md5(remote: Remote, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    out = remote.run(" ".join(["md5sum"] + [q(p) for p in paths]) + " 2>/dev/null", check=False).text
    sums = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            sums[parts[1].strip()] = parts[0]
    return sums


# ------------------------------------------------------------- local state


def state_file() -> Path:
    return config.DATA_DIR / "tablet-changes.json"


def load_state() -> dict:
    try:
        value = json.loads(state_file().read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    config.write_json_atomic(state_file(), state)


def remember(section: str, key: str, value: dict | None) -> None:
    state = load_state()
    bucket = state.setdefault(section, {})
    if value is None:
        bucket.pop(key, None)
    else:
        bucket[key] = {**value, "at": time.time()}
    save_state(state)


# ------------------------------------------------------------- images


def fit_png(src: Path, width: int = SCREEN_W, height: int = SCREEN_H) -> tuple[bytes, dict]:
    """The image as a PNG of exactly width x height, letterboxed on white.

    Returns the bytes and what was done, so the caller can say so.
    """
    import pymupdf

    try:
        with pymupdf.open(src) as doc:
            page = doc[0]
            if doc.is_pdf or src.suffix.lower() == ".svg":
                srcw, srch = page.rect.width, page.rect.height
            else:
                pix = pymupdf.Pixmap(src)
                srcw, srch = pix.width, pix.height
    except Exception as e:
        raise ValueError(f"can't read {src.name} as an image") from e
    info = {"source": [int(srcw), int(srch)], "size": [width, height]}
    if (int(srcw), int(srch)) == (width, height) and src.suffix.lower() == ".png":
        info["converted"] = False
        return src.read_bytes(), info
    out = pymupdf.open()
    page = out.new_page(width=width, height=height)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    if src.suffix.lower() in (".svg", ".pdf"):
        with pymupdf.open(src) as doc:
            pdf = pymupdf.open("pdf", doc.convert_to_pdf()) if not doc.is_pdf else doc
            page.show_pdf_page(page.rect, pdf, 0, keep_proportion=True)
    else:
        page.insert_image(page.rect, filename=str(src), keep_proportion=True)
    pix = page.get_pixmap(alpha=False)
    data = pix.tobytes("png")
    out.close()
    info["converted"] = True
    return data, info


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


# ------------------------------------------------------------- sleep screen


def carousel_files(remote: Remote) -> list[str]:
    out = remote.run(f"ls -1 {q(CAROUSEL_DIR)} 2>/dev/null", check=False).text
    return [f"{CAROUSEL_DIR}/{n.strip()}" for n in out.splitlines()
            if n.strip().lower().endswith(".png") and n.strip().startswith("sleep_Illustration_")]


def sleepscreen_status(remote: Remote) -> dict:
    fw = firmware(remote)
    carousel = carousel_files(remote)
    sums = remote_md5(remote, [SUSPENDED] + carousel)
    stock = original(remote, fw, SUSPENDED)
    current = sums.get(SUSPENDED)
    mine = load_state().get("sleepscreen", {}).get("suspended", {})
    if current is None:
        kind = "missing"
    elif stock is not None and md5(stock) == current:
        kind = "stock"
    elif mine.get("md5") == current:
        kind = "custom"
    elif stock is None:
        kind = "stock"  # never changed by us on this firmware
    else:
        kind = "other"
    changed_carousel = []
    for path in carousel:
        orig = original(None, fw, path)
        if orig is not None and md5(orig) != sums.get(path):
            changed_carousel.append(path)
    return {"firmware": fw, "path": SUSPENDED, "state": kind, "md5": current,
            "backupExists": stock is not None, "carousel": carousel, "carouselChanged": changed_carousel}


def sleepscreen_plan(image: Path, carousel: bool) -> tuple[bytes, dict, list[str]]:
    data, info = fit_png(image)
    plan = [f"replace {SUSPENDED} with {image.name}"
            + (f" (resized from {info['source'][0]}×{info['source'][1]} to {SCREEN_W}×{SCREEN_H})"
               if info.get("converted") else "")]
    if carousel:
        plan.append(f"replace every {CAROUSEL_DIR}/sleep_Illustration_*.png with the same picture")
    plan.append("back up the original files first (on the tablet and on this computer)")
    return data, info, plan


def sleepscreen_set(remote: Remote, data: bytes, *, carousel: bool, restart: bool) -> dict:
    fw = firmware(remote)
    targets = [SUSPENDED] + (carousel_files(remote) if carousel else [])
    backup_originals(remote, fw, targets)
    result = apply(remote, [Change(t, data) for t in targets], restart=restart)
    remember("sleepscreen", "suspended", {"md5": md5(data), "firmware": fw, "carousel": targets[1:]})
    return {"firmware": fw, "replaced": targets, **result}


def sleepscreen_restore(remote: Remote, *, restart: bool) -> dict:
    fw = firmware(remote)
    targets = [SUSPENDED] + carousel_files(remote)
    changes = []
    for path in targets:
        stock = original(remote, fw, path)
        if stock is not None:
            changes.append(Change(path, stock))
    if not changes:
        return {"firmware": fw, "restored": [], "applied": False,
                "note": "No backup for this firmware, so the stock sleep screen is already in place."}
    current = remote_md5(remote, [c.target for c in changes])
    changes = [c for c in changes if current.get(c.target) != md5(c.data or b"")]
    if not changes:
        remember("sleepscreen", "suspended", None)
        return {"firmware": fw, "restored": [], "applied": False, "note": "Already the stock sleep screen."}
    result = apply(remote, changes, restart=restart)
    remember("sleepscreen", "suspended", None)
    return {"firmware": fw, "restored": [c.target for c in changes], **result}
