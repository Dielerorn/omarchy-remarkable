"""Typed text and highlights out of documents, as Obsidian-friendly Markdown.

One note per document under settings["notesDir"], mirroring the cloud folder
path. Everything below MARKER in a note is the user's own and survives every
re-sync; everything above it is regenerated.
"""

from __future__ import annotations

import argparse
import colorsys
import filecmp
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from rmscene import scene_items as si
from rmscene.text import TextDocument

from . import cloud, config, library, render, transcribe
from .doc import Document

MARKER = "<!-- your notes below -->"
IMAGE_WIDTH = 1240  # same width the library view renders, so the cache is shared
ATTACHMENTS = "attachments"

log = logging.getLogger(__name__)

PS = si.ParagraphStyle
LIST_STYLES = {PS.BULLET, PS.BULLET2, PS.CHECKBOX, PS.CHECKBOX_CHECKED}


@dataclass
class Highlight:
    page: int  # 0-based page index
    text: str
    color: str
    rgba: tuple[int, int, int, int]

    @property
    def hex(self) -> str:
        return "#{:02x}{:02x}{:02x}".format(*self.rgba[:3])

    def to_json(self) -> dict:
        return {"page": self.page + 1, "text": self.text, "color": self.color, "hex": self.hex}


@dataclass
class PageNotes:
    index: int
    typed: str = ""  # Markdown
    plain: str = ""  # the same text without Markdown, for search
    highlights: list[Highlight] = field(default_factory=list)
    ink: bool = False  # has pen strokes
    handwriting: str = ""  # cached transcription, if any

    @property
    def has_text(self) -> bool:
        return bool(self.typed or self.highlights or self.handwriting)


# -------------------------------------------------------------- typed text


def _paragraphs(tree) -> list[tuple[int, list]]:
    text = getattr(tree, "root_text", None) if tree is not None else None
    if text is None:
        return []
    try:
        doc = TextDocument.from_scene_item(text)
    except Exception:  # odd CRDT state on one page shouldn't sink the note
        return []
    return [(int(p.style.value), p.contents) for p in doc.contents]


def _inline(spans: list) -> str:
    out = []
    for span in spans:
        s = str(span).replace(" ", "\n")
        core = s.strip()
        if not core:
            out.append(s)
            continue
        # Emphasis markers must hug the text, so keep surrounding spaces outside.
        lead, trail = s[: len(s) - len(s.lstrip())], s[len(s.rstrip()):]
        props = getattr(span, "properties", {}) or {}
        bold = props.get("font-weight") == "bold"
        italic = props.get("font-style") == "italic"
        if bold and italic:
            core = f"***{core}***"
        elif bold:
            core = f"**{core}**"
        elif italic:
            core = f"*{core}*"
        out.append(lead + core + trail)
    return "".join(out)


def typed_markdown(tree) -> str:
    """The page's typed text as Markdown (headings, lists, checkboxes, bold)."""
    blocks: list[str] = []
    in_list = False
    for style, spans in _paragraphs(tree):
        body = _inline(spans).strip()
        if not body:
            in_list = False
            continue
        if style == PS.HEADING:
            line = "### " + " ".join(body.replace("*", "").split())
        elif style == PS.BOLD:
            line = body if body.startswith("**") else "\n".join(f"**{l.strip()}**" if l.strip() else ""
                                                                  for l in body.split("\n"))
        elif style == PS.BULLET:
            line = "- " + body
        elif style == PS.BULLET2:
            line = "  - " + body
        elif style == PS.CHECKBOX:
            line = "- [ ] " + body
        elif style == PS.CHECKBOX_CHECKED:
            line = "- [x] " + body
        else:
            line = "\n".join(l.strip() for l in body.split("\n"))
        if style in LIST_STYLES and in_list:
            blocks[-1] += "\n" + line
        else:
            blocks.append(line)
        in_list = style in LIST_STYLES
    return "\n\n".join(blocks)


def typed_plain(tree) -> str:
    lines = []
    for _style, spans in _paragraphs(tree):
        s = "".join(str(x) for x in spans).replace(" ", "\n").strip()
        if s:
            lines.append(s)
    return "\n".join(lines)


# -------------------------------------------------------------- highlights


def color_name(rgba) -> str:
    """A plain colour word for a highlighter colour."""
    r, g, b = (c / 255 for c in rgba[:3])
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.15:
        return "gray"
    deg = h * 360
    for limit, name in ((15, "red"), (45, "orange"), (70, "yellow"), (165, "green"), (255, "blue"),
                        (290, "purple"), (345, "pink")):
        if deg < limit:
            return name
    return "red"


def _rgba(item) -> tuple[int, int, int, int]:
    rgba = getattr(item, "color_rgba", None)
    if rgba is None:
        rgb = render.PALETTE.get(getattr(item, "color", None), render.PALETTE[si.PenColor.YELLOW])
        rgba = (*rgb, 255)
    return tuple(int(c) for c in rgba)  # type: ignore[return-value]


def page_highlights(tree, index: int) -> list[Highlight]:
    """Highlights on one page, in reading order, one per continuous passage.

    The tablet stores a multi-line highlight as one range per line (and
    sometimes the same range several times); ranges of one colour that touch
    in the text are joined back into a single passage.
    """
    if tree is None:
        return []
    raw = []
    for item in tree.walk():
        if not isinstance(item, si.GlyphRange) or not (item.text or "").strip():
            continue
        rect = item.rectangles[0] if item.rectangles else None
        pos = (rect.y, rect.x) if rect is not None else (0.0, 0.0)
        raw.append((item.start, item.length or len(item.text), item.text, _rgba(item), pos))
    if not raw:
        return []
    passages: list[list] = []  # [start, end, text, rgba]
    if all(r[0] is not None for r in raw):
        for start, length, text, rgba, _pos in sorted(raw, key=lambda r: (r[0], -r[1])):
            end = start + length
            last = passages[-1] if passages else None
            if last and last[3] == rgba and start <= last[1] + 3:
                if end <= last[1]:
                    continue  # duplicate or inside the previous range
                if start >= last[1]:
                    joined = last[2].rstrip("­")
                    sep = "" if start == last[1] or joined.endswith("-") else " "
                    last[2] = joined + sep + text
                else:
                    last[2] += text[last[1] - start:]
                last[1] = end
            else:
                passages.append([start, end, text, rgba])
    else:
        seen = set()
        for start, length, text, rgba, pos in sorted(raw, key=lambda r: r[4]):
            if (text, rgba, pos) not in seen:
                seen.add((text, rgba, pos))
                passages.append([0, 0, text, rgba])
    return [Highlight(index, " ".join(p[2].split()), color_name(p[3]), p[3]) for p in passages]


def document_highlights(doc: Document) -> list[Highlight]:
    out: list[Highlight] = []
    for page in doc.pages:
        if page.rm:
            out.extend(page_highlights(render.read_scene(page), page.index))
    return out


def highlight_markdown(h: Highlight) -> str:
    return f"> {h.text}\n> #highlight/{h.color}"


def highlights_markdown(doc: Document, highlights: list[Highlight]) -> str:
    parts = [f"# {doc.name}"]
    page = None
    for h in highlights:
        if h.page != page:
            parts.append(f"## Page {h.page + 1}")
            page = h.page
        parts.append(highlight_markdown(h))
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------- extract


def extract(doc: Document, transcripts: dict[int, str] | None = None) -> list[PageNotes]:
    """What each annotated page has to say. Pages without annotations are left out."""
    if transcripts is None:
        transcripts = transcribe.cached(doc)
    out = []
    for page in doc.pages:
        if not page.rm:
            continue
        tree = render.read_scene(page)
        out.append(PageNotes(
            index=page.index, typed=typed_markdown(tree), plain=typed_plain(tree),
            highlights=page_highlights(tree, page.index), ink=transcribe.has_writing(tree),
            handwriting=transcripts.get(page.index, ""),
        ))
    return out


# ------------------------------------------------------------------- notes


def doc_tags(doc: Document) -> list[str]:
    # doc.tags trips over "tags": null in older .content files.
    raw = doc.content.get("tags") or []
    names = [t.get("name", "") for t in raw if isinstance(t, dict)]
    out = []
    for name in names:
        tag = re.sub(r"[^\w/-]+", "-", name.strip().lstrip("#")).strip("-")
        if tag and tag not in out:
            out.append(tag)
    return out


def _yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid YAML double-quoted scalar


def frontmatter(entry: cloud.Entry, doc: Document) -> str:
    tags = ["remarkable"] + [t for t in doc_tags(doc) if t != "remarkable"]
    lines = [
        "---",
        f"title: {_yaml_str(doc.name)}",
        "source: reMarkable",
        f"remarkable_id: {doc.id}",
        f"path: {_yaml_str(entry.path)}",
        f"modified: {entry.modified}" if entry.modified else "modified:",
        f"type: {doc.file_type}",
        f"pages: {len(doc.pages)}",
        "tags:",
        *[f"  - {_yaml_str(t) if not re.fullmatch(r'[A-Za-z0-9_/-]+', t) else t}" for t in tags],
        "---",
    ]
    return "\n".join(lines)


def demote_headings(md: str, levels: int = 2) -> str:
    """Push headings below the note's "## Page N" level."""
    out, fenced = [], False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        m = None if fenced else re.match(r"^(#{1,6})(\s+\S.*)$", line)
        out.append("#" * min(6, len(m.group(1)) + levels) + m.group(2) if m else line)
    return "\n".join(out)


def page_section(p: PageNotes, image: str | None) -> str:
    parts = []
    if p.typed:
        parts.append(p.typed)
    if p.handwriting:
        parts.append(("**Handwriting**\n\n" if p.typed else "") + demote_headings(p.handwriting))
    parts.extend(highlight_markdown(h) for h in p.highlights)
    if image:
        parts.append(f"![[{image}]]")
    if not parts:
        return ""
    return f"## Page {p.index + 1}\n\n" + "\n\n".join(parts)


def render_note(entry: cloud.Entry, doc: Document, pages: list[PageNotes], images: dict[int, str],
                preserved: str = "") -> str:
    sections = [s for s in (page_section(p, images.get(p.index)) for p in pages) if s]
    body = "\n\n".join([frontmatter(entry, doc), *sections, MARKER])
    preserved = preserved.strip("\n")
    return body + "\n" + (preserved + "\n" if preserved else "")


_ID_LINE = re.compile(r"^remarkable_id:\s*\"?([0-9a-fA-F-]{8,})\"?\s*$", re.M)


def note_id(path: Path) -> str | None:
    """The remarkable_id in a note's frontmatter, if it's one of ours."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(2048)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    m = _ID_LINE.search(head.split("\n---", 1)[0])
    return m.group(1) if m else None


def note_index(notes_dir: Path) -> dict[str, Path]:
    """Every note we wrote, by document id (so moved documents keep their note)."""
    index: dict[str, Path] = {}
    if not notes_dir.is_dir():
        return index
    for path in notes_dir.rglob("*.md"):
        if ATTACHMENTS in path.relative_to(notes_dir).parts[:1]:
            continue
        doc_id = note_id(path)
        if doc_id and doc_id not in index:
            index[doc_id] = path
    return index


def preserved_text(path: Path, doc_id: str) -> str:
    """The user's part of an existing note."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if MARKER in text:
        return text.split(MARKER, 1)[1].lstrip("\n")
    # No marker (deleted by hand?): keep the old body rather than lose edits.
    if note_id(path) == doc_id and text.startswith("---"):
        parts = text.split("\n---", 1)
        text = parts[1].split("\n", 1)[1] if len(parts) == 2 and "\n" in parts[1] else ""
    return text.strip("\n")


def note_target(notes_dir: Path, entry: cloud.Entry) -> Path:
    segments = [library.safe_name(s) for s in entry.path.strip("/").split("/") if s] or [library.safe_name(entry.name)]
    folder = notes_dir.joinpath(*segments[:-1])
    target = folder / f"{segments[-1]}.md"
    if target.exists() and note_id(target) != entry.id:
        # Two documents with one name in one folder, or a file that isn't ours.
        target = folder / f"{segments[-1]} ({entry.id[:8]}).md"
    return target


def export_images(doc: Document, pages: list[PageNotes], notes_dir: Path) -> dict[int, str]:
    """Copy rendered pages with ink into <notesDir>/attachments; drop stale ones."""
    folder = notes_dir / ATTACHMENTS
    short = doc.id[:8]
    base = library.safe_name(doc.name)
    names: dict[int, str] = {}
    for p in pages:
        if not p.ink:
            continue
        name = f"{base} {short} p{p.index + 1}.png"
        src = render.page_png(doc, p.index, width=IMAGE_WIDTH)
        dst = folder / name
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            folder.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(f".{name}.tmp")
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
        names[p.index] = name
    if folder.is_dir():
        mine = re.compile(rf" {re.escape(short)} p\d+\.png$")
        keep = set(names.values())
        for f in folder.iterdir():
            if mine.search(f.name) and f.name not in keep:
                f.unlink(missing_ok=True)
    return names


def _write_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == text:
            os.utime(path)  # mark it synced so the daemon doesn't keep revisiting it
            return
    except (OSError, UnicodeDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def sync_document(doc_id: str, *, settings: dict | None = None, index: dict[str, Path] | None = None,
                  entry: cloud.Entry | None = None, always: bool = False) -> Path | None:
    """Write (or refresh) one document's note.

    Returns None when the document has nothing to say and has no note yet,
    unless always=True (someone asked for this note specifically).
    """
    settings = settings or config.load_settings()
    notes_dir = config.setting_path(settings, "notesDir")
    cached_entry, doc = library.open_document(doc_id)
    entry = entry or cached_entry
    if entry.path == "/" + cloud.TRASH or entry.path.startswith("/" + cloud.TRASH + "/"):
        raise ValueError(f"{doc.name} is in the reMarkable trash")
    pages = extract(doc)
    if index is None:
        index = note_index(notes_dir)
    current = index.get(doc.id)
    target = note_target(notes_dir, entry)
    if current is not None and current != target and current.exists() and not target.exists():
        # The document was moved or renamed in the cloud: take the note along.
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(current, target)
    if not always and not any(p.has_text or p.ink for p in pages) and not target.exists():
        return None
    images = export_images(doc, pages, notes_dir) if settings.get("notesIncludeImages", True) else {}
    if not always and not any(p.has_text or p.index in images for p in pages) and not target.exists():
        return None
    preserved = preserved_text(target, doc.id) if target.exists() else ""
    _write_if_changed(target, render_note(entry, doc, pages, images, preserved))
    index[doc.id] = target
    return target


def sync_changed(lib: cloud.Library, state: dict) -> list[Path]:
    """Daemon hook: refresh notes whose documents changed since the last sync.

    Covers documents that already have a note plus everything inside
    settings["autoTranscribeFolders"]. state["notesSynced"] maps id ->
    the modified time last synced. A note is also refreshed when new
    transcripts landed after it was written.
    """
    settings = config.load_settings()
    notes_dir = config.setting_path(settings, "notesDir")
    folders = settings.get("autoTranscribeFolders") or []
    synced: dict = state.setdefault("notesSynced", {})
    index = note_index(notes_dir)
    written: list[Path] = []
    docs = lib.documents()
    for e in docs:
        note = index.get(e.id)
        if note is None and not transcribe.in_folders(e.path, folders):
            continue
        stale = synced.get(e.id) != e.modified
        if not stale and note is not None:
            try:
                stale = transcribe.last_change(e.id) > note.stat().st_mtime
            except OSError:
                stale = True
        if not stale:
            continue
        try:
            path = sync_document(e.id, settings=settings, index=index, entry=e)
        except Exception as err:  # one bad document mustn't stop the rest
            log.warning("notes for %s: %s", e.path, err)
            continue
        synced[e.id] = e.modified
        if path is not None:
            written.append(path)
    live = {e.id for e in docs}
    for doc_id in [k for k in synced if k not in live]:
        del synced[doc_id]
    return written


# ---------------------------------------------------------------- commands


def cmd_highlights(args: argparse.Namespace) -> dict:
    """Per page: typed text, highlights and any cached handwriting transcript."""
    e, doc = library.open_document(args.id)
    pages = extract(doc)
    found = [h for p in pages for h in p.highlights]
    result = {"ok": True, "id": doc.id, "name": doc.name, "path": e.path, "count": len(found)}
    if args.format == "json":
        result["pages"] = [
            {"index": p.index, "typed": p.typed, "handwriting": p.handwriting,
             "highlights": [{"text": h.text, "color": h.color, "hex": h.hex} for h in p.highlights]}
            for p in pages if p.has_text
        ]
    else:
        result["markdown"] = highlights_markdown(doc, found) if found else ""
    return result


def cmd_sync(args: argparse.Namespace) -> dict:
    from .cli import progress
    settings = config.load_settings()
    notes_dir = config.setting_path(settings, "notesDir")
    if args.id:
        path = sync_document(args.id, settings=settings, always=True)
        return {"ok": True, "path": str(path), "dir": str(notes_dir)}
    if not (args.all or args.folder):
        return {"ok": False, "error": "say which: notes sync <id> | --all | --folder PATH"}
    lib = library.fetch_library()
    docs = lib.documents()
    if args.folder:
        folder = "/" + args.folder.strip().strip("/")
        if folder != "/" and lib.find_path(folder) is None:
            return {"ok": False, "error": f"no folder {folder} on the tablet"}
        docs = [e for e in docs if transcribe.in_folders(e.path, [folder])]
    docs.sort(key=lambda e: e.path)
    index = note_index(notes_dir)
    paths, failed, empty = [], [], 0
    for n, e in enumerate(docs, 1):
        progress(f"notes: {e.path}", n - 1, len(docs))
        try:
            path = sync_document(e.id, settings=settings, index=index, entry=e)
        except (cloud.CloudError, ValueError, OSError) as err:
            failed.append({"id": e.id, "path": e.path, "error": str(err)})
            continue
        if path is None:
            empty += 1
        else:
            paths.append(str(path))
    progress("done", len(docs), len(docs))
    return {"ok": True, "written": len(paths), "dir": str(notes_dir), "paths": paths,
            "nothingToSay": empty, "failed": failed}


def register(sub) -> None:
    p = sub.add_parser("highlights", help="highlights in a PDF or EPUB, with their colours")
    p.add_argument("id")
    p.add_argument("--format", choices=["md", "json"], default="md")
    p.set_defaults(func=cmd_highlights)

    n = sub.add_parser("notes", help="Markdown notes (Obsidian-friendly) from documents")
    nsub = n.add_subparsers(dest="action", required=True)
    x = nsub.add_parser("sync", help="write or refresh notes in the notes folder")
    x.add_argument("id", nargs="?")
    x.add_argument("--all", action="store_true", help="every document that has something to say")
    x.add_argument("--folder", help="only documents in this cloud folder, e.g. /Books")
    x.set_defaults(func=cmd_sync)
