"""Send files from this computer to the tablet through the cloud."""

from __future__ import annotations

import argparse
import html
import os
import tempfile
from pathlib import Path

import pymupdf

from . import cloud, config

# Paper Pro screen: 1620x2160 px at 229 ppi.
PAGE_W = 1620 * 72 / 229
PAGE_H = 2160 * 72 / 229
DIRECT = {".pdf", ".epub"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
TEXT = {".txt", ".md", ".markdown", ".html", ".htm"}

READING_CSS = """
body { font-family: serif; font-size: 12pt; line-height: 1.45; color: #111; }
h1 { font-size: 20pt; margin: 0 0 8pt 0; }
h2 { font-size: 16pt; margin: 14pt 0 6pt 0; }
h3 { font-size: 13pt; margin: 12pt 0 4pt 0; }
p { margin: 0 0 7pt 0; text-align: justify; }
pre, code { font-family: monospace; font-size: 9.5pt; }
pre { background: #f2f2f2; padding: 6pt; }
blockquote { margin: 6pt 14pt; color: #333; font-style: italic; }
img { max-width: 100%; }
li { margin-bottom: 3pt; }
.meta { color: #555; font-size: 9pt; margin-bottom: 14pt; }
"""


def image_to_pdf(src: Path, target: Path) -> Path:
    doc = pymupdf.open()
    with pymupdf.open(src) as img:
        rect = img[0].rect
    landscape = rect.width > rect.height
    w, h = (PAGE_H, PAGE_W) if landscape else (PAGE_W, PAGE_H)
    page = doc.new_page(width=w, height=h)
    margin = 18
    page.insert_image(pymupdf.Rect(margin, margin, w - margin, h - margin), filename=str(src), keep_proportion=True)
    doc.save(target, deflate=True)
    doc.close()
    return target


def markdown_to_html(text: str) -> str:
    """Small Markdown subset: headings, lists, code blocks, emphasis, links."""
    import re
    out: list[str] = []
    in_code = False
    in_list = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        item = re.match(r"^\s*[-*+]\s+(.*)$", line) or re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if in_list and not item:
            out.append("</ul>")
            in_list = False
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(item.group(1))}</li>")
        elif line.startswith(">"):
            out.append(f"<blockquote>{_inline(line.lstrip('> '))}</blockquote>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def _inline(s: str) -> str:
    import re
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<i>\1</i>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", s)
    return s


def html_to_pdf(body_html: str, target: Path, *, css: str = READING_CSS, title: str = "",
                archive: str | None = None) -> Path:
    """Lay HTML out on tablet-sized pages with PyMuPDF's Story engine."""
    story = pymupdf.Story(html=f"<html><body>{body_html}</body></html>", user_css=css,
                          archive=archive)
    writer = pymupdf.DocumentWriter(str(target))
    mediabox = pymupdf.Rect(0, 0, PAGE_W, PAGE_H)
    where = mediabox + (40, 40, -40, -44)
    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    if title:
        with pymupdf.open(target) as d:
            d.set_metadata({"title": title, "creator": "omarchy-remarkable"})
            d.saveIncr()
    return target


def text_to_pdf(src: Path, target: Path) -> Path:
    text = src.read_text(errors="replace")
    suffix = src.suffix.lower()
    if suffix in (".md", ".markdown"):
        body = markdown_to_html(text)
    elif suffix in (".html", ".htm"):
        body = text
    else:
        body = "".join(f"<p>{html.escape(p)}</p>" for p in text.split("\n\n"))
    return html_to_pdf(body, target, title=src.stem, archive=str(src.parent))


def prepare(src: Path, workdir: Path) -> Path:
    """Return a file the cloud accepts (PDF/EPUB), converting if needed."""
    suffix = src.suffix.lower()
    if suffix in DIRECT:
        return src
    target = workdir / f"{src.stem}.pdf"
    if suffix in IMAGES:
        return image_to_pdf(src, target)
    if suffix in TEXT:
        return text_to_pdf(src, target)
    raise ValueError(f"can't send {src.suffix or 'this kind of'} files; PDF, EPUB, images, text and Markdown work")


def send_file(src: Path, folder: str | None = None, name: str | None = None) -> dict:
    settings = config.load_settings()
    folder = settings["uploadFolder"] if folder is None else folder
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as tmp:
        ready = prepare(src, Path(tmp))
        new = cloud.upload(ready, folder, name=name or src.stem)
    return {"file": str(src), "id": new.id if new else None, "name": new.name if new else (name or src.stem),
            "folder": folder}


def latest_screenshot() -> Path | None:
    folder = Path(os.environ.get("OMARCHY_SCREENSHOT_DIR") or os.environ.get("XDG_PICTURES_DIR") or Path.home() / "Pictures")
    candidates = [p for p in folder.glob("screenshot-*") if p.suffix.lower() in IMAGES]
    framelet = folder / "Framelet"
    if framelet.is_dir():
        candidates += [p for p in framelet.rglob("*") if p.suffix.lower() in IMAGES]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def cmd_send(args: argparse.Namespace) -> dict:
    from .cli import notify
    from .library import fetch_library
    results, errors = [], []
    for f in args.files:
        src = Path(f).expanduser()
        try:
            results.append(send_file(src, args.folder, args.name if len(args.files) == 1 else None))
        except (ValueError, cloud.CloudError, OSError) as e:
            errors.append({"file": str(src), "error": str(e)})
    if results:
        fetch_library()
    if args.notify:
        if results and not errors:
            names = ", ".join(r["name"] for r in results[:3]) + ("…" if len(results) > 3 else "")
            notify("Sent to reMarkable", names)
        elif errors:
            notify("Couldn't send to reMarkable", errors[0]["error"], urgency="critical")
    return {"ok": not errors, "sent": results, "errors": errors,
            **({"error": errors[0]["error"]} if errors and not results else {})}


def cmd_screenshot(args: argparse.Namespace) -> dict:
    from .cli import notify
    from .library import fetch_library
    shot = Path(args.file).expanduser() if args.file else latest_screenshot()
    if shot is None or not shot.exists():
        return {"ok": False, "error": "no screenshot found"}
    folder = config.load_settings()["screenshotFolder"]
    result = send_file(shot, folder)
    fetch_library()
    if args.notify:
        notify("Screenshot sent to reMarkable", result["name"])
    return {"ok": True, "sent": [result]}


def pick_files() -> list[str]:
    """Ask with the desktop's file chooser (zenity); [] when cancelled."""
    import shutil
    import subprocess
    if not shutil.which("zenity"):
        raise ValueError("no file chooser available (install zenity) — drag files onto the library window instead")
    patterns = " ".join(f"*{e} *{e.upper()}" for e in sorted(DIRECT | IMAGES | TEXT))
    proc = subprocess.run(
        ["zenity", "--file-selection", "--multiple", "--separator=\n", "--title=Send to reMarkable",
         f"--file-filter=Documents and images | {patterns}", "--file-filter=All files | *"],
        capture_output=True, text=True,
    )
    return [l for l in proc.stdout.splitlines() if l.strip()] if proc.returncode == 0 else []


def cmd_pick(args: argparse.Namespace) -> dict:
    """Just ask for a file (for sleep screens, templates); returns its path."""
    import shutil
    import subprocess
    if not shutil.which("zenity"):
        return {"ok": False, "error": "no file chooser available (install zenity)"}
    filters = {"image": "Images | *.png *.PNG *.jpg *.JPG *.jpeg *.JPEG",
               "template": "Templates | *.svg *.SVG *.png *.PNG",
               "any": "All files | *"}
    proc = subprocess.run(["zenity", "--file-selection", f"--title={args.title}",
                           f"--file-filter={filters.get(args.kind, filters['any'])}"],
                          capture_output=True, text=True)
    paths = [l for l in proc.stdout.splitlines() if l.strip()] if proc.returncode == 0 else []
    return {"ok": True, "paths": paths, "cancelled": not paths}


def cmd_send_pick(args: argparse.Namespace) -> dict:
    files = pick_files()
    if not files:
        return {"ok": True, "sent": [], "errors": [], "cancelled": True}
    args.files = files
    args.name = None
    return cmd_send(args)


def register(sub) -> None:
    p = sub.add_parser("pick", help="ask for a file with the desktop file chooser")
    p.add_argument("--kind", choices=["image", "template", "any"], default="any")
    p.add_argument("--title", default="Choose a file")
    p.set_defaults(func=cmd_pick)

    p = sub.add_parser("send-pick", help="choose files with the file chooser, then send them")
    p.add_argument("--folder")
    p.add_argument("--notify", action="store_true")
    p.set_defaults(func=cmd_send_pick)

    p = sub.add_parser("send", help="send files to the tablet (PDF, EPUB, images, text, Markdown)")
    p.add_argument("files", nargs="+")
    p.add_argument("--folder", help='cloud folder ("" = top level); default from settings')
    p.add_argument("--name", help="document name on the tablet (single file only)")
    p.add_argument("--notify", action="store_true", help="show a desktop notification when done")
    p.set_defaults(func=cmd_send)

    s = sub.add_parser("screenshot", help="send the newest screenshot (or a given image)")
    s.add_argument("file", nargs="?")
    s.add_argument("--notify", action="store_true")
    s.set_defaults(func=cmd_screenshot)
