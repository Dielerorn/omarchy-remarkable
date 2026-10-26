"""Draw reMarkable pages as vector PDF pages and PNG images.

Stroke styling and the text-anchor layout are adapted from rmc
(https://github.com/ricklupton/rmc, MIT, (c) 2023 Rick Lupton), itself from
maxio. Differences: output goes straight onto PyMuPDF pages instead of SVG,
pens keep their colour (the Paper Pro has colour pens, rmc greys ballpoints),
and the page size comes from the file's SceneInfo (1620x2160 on a Paper Pro).
"""

from __future__ import annotations

import hashlib
import logging
import math
import warnings
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from rmscene import CrdtId, read_tree
from rmscene import scene_items as si
from rmscene.text import TextDocument

from . import config
from .doc import Document, Page, rm_stream

logging.getLogger("rmscene").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", module="rmscene")

DPI = 226  # the unit rmscene coordinates are in, for PDFs too (matches rmapi)
SCALE = 72.0 / DPI
DEFAULT_PAPER = (1404, 1872)
TEMPLATE_DIR = config.CACHE_DIR / "templates"

TEXT_TOP_Y = -88
LINE_HEIGHTS = {
    si.ParagraphStyle.PLAIN: 70,
    si.ParagraphStyle.BULLET: 35,
    si.ParagraphStyle.BULLET2: 35,
    si.ParagraphStyle.BOLD: 70,
    si.ParagraphStyle.HEADING: 150,
    si.ParagraphStyle.CHECKBOX: 35,
    si.ParagraphStyle.CHECKBOX_CHECKED: 35,
}
TOP_ANCHOR = CrdtId(0, 281474976710654)
BOTTOM_ANCHOR = CrdtId(0, 281474976710655)

PALETTE = {
    si.PenColor.BLACK: (0, 0, 0),
    si.PenColor.GRAY: (144, 144, 144),
    si.PenColor.WHITE: (255, 255, 255),
    si.PenColor.YELLOW: (251, 247, 25),
    si.PenColor.GREEN: (0, 180, 0),
    si.PenColor.PINK: (255, 105, 180),
    si.PenColor.BLUE: (78, 105, 201),
    si.PenColor.RED: (179, 62, 57),
    si.PenColor.GRAY_OVERLAP: (125, 125, 125),
    si.PenColor.GREEN_2: (161, 216, 125),
    si.PenColor.CYAN: (139, 208, 229),
    si.PenColor.MAGENTA: (183, 130, 205),
    si.PenColor.YELLOW_2: (247, 232, 81),
}
HIGHLIGHT_DEFAULT = (250, 221, 25, 140)


def _clamp(v: float) -> float:
    return min(max(v, 0.0), 1.0)


def _tilt(direction: float) -> float:
    return direction * (math.pi * 2) / 255


@dataclass
class Seg:
    width: float  # device px
    rgb: tuple[float, float, float]  # 0..1
    opacity: float


class Pen:
    segment_length = 1000
    cap = 1  # round
    opacity = 1.0

    def __init__(self, base_width: float, rgba: tuple[int, int, int, int]):
        self.base_width = base_width
        self.rgba = rgba

    @property
    def rgb(self) -> tuple[float, float, float]:
        return (self.rgba[0] / 255, self.rgba[1] / 255, self.rgba[2] / 255)

    def segment(self, p, last_width: float) -> Seg:
        return Seg(self.width(p, last_width), self.color(p), self.opacity_for(p))

    def width(self, p, last_width: float) -> float:
        return self.base_width

    def color(self, p) -> tuple[float, float, float]:
        return self.rgb

    def opacity_for(self, p) -> float:
        return self.opacity


class Fineliner(Pen):
    def __init__(self, w, c):
        super().__init__(w * 1.8, c)


class Ballpoint(Pen):
    segment_length = 5

    def width(self, p, last_width):
        return (0.5 + p.pressure / 255) + (p.width / 4) - 0.5 * ((p.speed / 4) / 50)

    def color(self, p):
        intensity = _clamp((0.1 * -((p.speed / 4) / 35)) + (1.2 * p.pressure / 255) + 0.5)
        if self.rgba[:3] == (0, 0, 0):
            g = min(int(abs(intensity - 1) * 255), 60) / 255
            return (g, g, g)
        # Colour ink: lighter strokes fade toward paper rather than toward grey.
        fade = 0.35 * (1 - intensity)
        return tuple(c + (1 - c) * fade for c in self.rgb)  # type: ignore[return-value]


class Marker(Pen):
    segment_length = 3

    def width(self, p, last_width):
        return 0.9 * ((p.width / 4) - 0.4 * _tilt(p.direction)) + (0.1 * last_width)


class Pencil(Pen):
    segment_length = 2

    def width(self, p, last_width):
        w = 0.7 * ((((0.8 * self.base_width) + (0.5 * p.pressure / 255)) * (p.width / 4))
                   - (0.25 * _tilt(p.direction) ** 1.8) - (0.6 * (p.speed / 4) / 50))
        return min(w, self.base_width * 10)

    def opacity_for(self, p):
        return max(0.05, _clamp((0.1 * -((p.speed / 4) / 35)) + (1 * p.pressure / 255)) - 0.1)


class MechanicalPencil(Pen):
    opacity = 0.7

    def __init__(self, w, c):
        super().__init__(w ** 2, c)


class Brush(Pen):
    segment_length = 2

    def width(self, p, last_width):
        return 0.7 * (((1 + (1.4 * p.pressure / 255)) * (p.width / 4))
                      - (0.5 * _tilt(p.direction)) - ((p.speed / 4) / 50))

    def color(self, p):
        intensity = _clamp(((p.pressure / 255) ** 1.5 - 0.2 * ((p.speed / 4) / 50)) * 1.5)
        fade = abs(intensity - 1)
        return tuple(c + (1 - c) * fade for c in self.rgb)  # type: ignore[return-value]


class Calligraphy(Pen):
    segment_length = 2

    def width(self, p, last_width):
        return 0.9 * (((1 + p.pressure / 255) * (p.width / 4)) - 0.3 * _tilt(p.direction)) + (0.1 * last_width)


class Highlighter(Pen):
    cap = 2  # square

    def __init__(self, w, c):
        super().__init__(w, c)
        self.opacity = (c[3] / 255) * 0.7 if c[3] < 255 else 0.35

    def width(self, p, last_width):
        return max(p.width / 4 * 1.0, 20.0) if p.width else 30.0


class Shader(Pen):
    def __init__(self, w, c):
        super().__init__(w, c)
        self.opacity = (c[3] / 255) * 0.6 if c[3] < 255 else 0.15

    def width(self, p, last_width):
        return max(p.width / 4, 12.0) if p.width else 12.0


class Eraser(Pen):
    cap = 2

    def __init__(self, w, c):
        super().__init__(w * 2, (255, 255, 255, 255))


def make_pen(line: si.Line) -> Pen | None:
    rgba = getattr(line, "color_rgba", None) or getattr(line, "rgba", None)
    if rgba is None:
        if line.color == si.PenColor.HIGHLIGHT:
            rgba = HIGHLIGHT_DEFAULT
        else:
            rgb = PALETTE.get(line.color, (0, 0, 0))
            rgba = (*rgb, 255)
    rgba = tuple(int(c) for c in rgba)  # type: ignore[assignment]
    w = line.thickness_scale
    t = si.Pen
    tool = line.tool
    if tool in (t.PAINTBRUSH_1, t.PAINTBRUSH_2):
        return Brush(w, rgba)
    if tool == t.CALIGRAPHY:
        return Calligraphy(w, rgba)
    if tool in (t.MARKER_1, t.MARKER_2):
        return Marker(w, rgba)
    if tool in (t.BALLPOINT_1, t.BALLPOINT_2):
        return Ballpoint(w, rgba)
    if tool in (t.FINELINER_1, t.FINELINER_2):
        return Fineliner(w, rgba)
    if tool in (t.PENCIL_1, t.PENCIL_2):
        return Pencil(w, rgba)
    if tool in (t.MECHANICAL_PENCIL_1, t.MECHANICAL_PENCIL_2):
        return MechanicalPencil(w, rgba)
    if tool in (t.HIGHLIGHTER_1, t.HIGHLIGHTER_2):
        return Highlighter(w, rgba)
    if tool == t.SHADER:
        return Shader(w, rgba)
    if tool == t.ERASER:
        return Eraser(w, rgba)
    if tool == t.ERASER_AREA:
        return None
    return Fineliner(w, rgba)


# --------------------------------------------------------------- scene layout


def read_scene(page: Page):
    if not page.rm:
        return None
    try:
        return read_tree(rm_stream(page))
    except Exception as e:  # a page we can't parse shouldn't kill the document
        logging.getLogger(__name__).warning("page %s: %s", page.id, e)
        return None


def paper_size(tree) -> tuple[int, int]:
    info = getattr(tree, "scene_info", None)
    size = getattr(info, "paper_size", None) if info is not None else None
    if size and size[0] and size[1]:
        return int(size[0]), int(size[1])
    return DEFAULT_PAPER


def anchor_positions(text, paper_h: int) -> dict:
    anchors: dict = {}
    if text is not None:
        doc = TextDocument.from_scene_item(text)
        y = text.pos_y + TEXT_TOP_Y
        top = y
        for p in doc.contents:
            anchors[p.start_id] = y
            for sub in p.contents:
                for k in sub.i:
                    anchors[k] = y
            y += LINE_HEIGHTS.get(p.style.value, 70)
        anchors[TOP_ANCHOR] = top
        anchors[BOTTOM_ANCHOR] = y
    else:
        anchors[TOP_ANCHOR] = 0
        anchors[BOTTOM_ANCHOR] = paper_h
    return anchors


def _anchor(group: si.Group, anchors: dict) -> tuple[float, float]:
    ax = ay = 0.0
    if getattr(group, "anchor_id", None) is not None and group.anchor_origin_x is not None:
        ax = group.anchor_origin_x.value
        ay = anchors.get(group.anchor_id.value, 0.0)
    return ax, ay


def _visible(group: si.Group) -> bool:
    vis = getattr(group, "visible", None)
    value = getattr(vis, "value", vis)
    return value is not False


def walk_lines(group: si.Group, anchors: dict, ox: float = 0.0, oy: float = 0.0):
    """Yield (line, x_offset, y_offset) in drawing order."""
    if not _visible(group):
        return
    ax, ay = _anchor(group, anchors)
    ox, oy = ox + ax, oy + ay
    for child in group.children.values():
        if isinstance(child, si.Group):
            yield from walk_lines(child, anchors, ox, oy)
        elif isinstance(child, si.Line):
            yield child, ox, oy


def bounds(tree, anchors: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for line, ox, oy in walk_lines(tree.root, anchors):
        for p in line.points:
            xs.append(p.x + ox)
            ys.append(p.y + oy)
    if not xs:
        return 0, 0, 0, 0
    return min(xs), max(xs), min(ys), max(ys)


def highlights(tree) -> list[tuple[str, list[tuple[float, float, float, float]], tuple[int, int, int, int]]]:
    """PDF/EPUB text highlights: (text, rects in device units, rgba)."""
    out = []
    for item in tree.walk():
        if isinstance(item, si.GlyphRange):
            rgba = getattr(item, "color_rgba", None) or getattr(item, "rgba", None)
            if rgba is None:
                rgb = PALETTE.get(getattr(item, "color", si.PenColor.YELLOW), PALETTE[si.PenColor.YELLOW])
                rgba = (*rgb, 140)
            rects = [(r.x, r.y, r.w, r.h) for r in item.rectangles]
            out.append((item.text or "", rects, tuple(int(c) for c in rgba)))
    return out


# ------------------------------------------------------------------- drawing


def draw_strokes(page: pymupdf.Page, tree, x_shift: float, y_shift: float = 0.0) -> None:
    """Draw a scene onto a PDF page. Device (x, y) -> (x*SCALE + x_shift, y*SCALE + y_shift)."""
    w, h = paper_size(tree)
    anchors = anchor_positions(tree.root_text, h)

    # Text highlights first, so ink sits on top of them.
    shape = page.new_shape()
    for _text, rects, rgba in highlights(tree):
        for (x, y, rw, rh) in rects:
            r = pymupdf.Rect(x * SCALE + x_shift, y * SCALE + y_shift,
                             (x + rw) * SCALE + x_shift, (y + rh) * SCALE + y_shift)
            shape.draw_rect(r)
            shape.finish(color=None, fill=(rgba[0] / 255, rgba[1] / 255, rgba[2] / 255),
                         fill_opacity=min(0.45, rgba[3] / 255))
    shape.commit()

    if tree.root_text is not None:
        draw_text(page, tree.root_text, x_shift, y_shift)

    shape = page.new_shape()
    for line, ox, oy in walk_lines(tree.root, anchors):
        pen = make_pen(line)
        if pen is None or not line.points:
            continue
        pts = line.points
        last_width = 0.0
        seg: Seg | None = None
        run: list[pymupdf.Point] = []

        def flush():
            if seg is None or len(run) < 1:
                return
            points = run if len(run) > 1 else [run[0], run[0] + (0.01, 0.01)]
            shape.draw_polyline(points)
            shape.finish(color=seg.rgb, width=max(seg.width * SCALE, 0.15), closePath=False,
                         lineCap=pen.cap, lineJoin=1, stroke_opacity=_clamp(seg.opacity))

        for i, p in enumerate(pts):
            xy = pymupdf.Point((p.x + ox) * SCALE + x_shift, (p.y + oy) * SCALE + y_shift)
            if i % pen.segment_length == 0:
                if run:
                    last = run[-1]
                    flush()
                    run = [last]
                seg = pen.segment(p, last_width)
                last_width = seg.width
            run.append(xy)
        flush()
    shape.commit()


def draw_text(page: pymupdf.Page, text: si.Text, x_shift: float, y_shift: float) -> None:
    try:
        doc = TextDocument.from_scene_item(text)
    except Exception:
        return
    y = TEXT_TOP_Y
    width = (getattr(text, "width", 0) or 1000) * SCALE
    for p in doc.contents:
        y += LINE_HEIGHTS.get(p.style.value, 70)
        s = str(p).strip()
        if not s:
            continue
        style = p.style.value
        size = 14 if style == si.ParagraphStyle.HEADING else 8.5
        font = "hebo" if style in (si.ParagraphStyle.BOLD, si.ParagraphStyle.HEADING) else "helv"
        if style in (si.ParagraphStyle.BULLET, si.ParagraphStyle.BULLET2):
            s = "• " + s
        elif style == si.ParagraphStyle.CHECKBOX:
            s = "☐ " + s
        elif style == si.ParagraphStyle.CHECKBOX_CHECKED:
            s = "☑ " + s
        x = text.pos_x * SCALE + x_shift
        top = (text.pos_y + y) * SCALE + y_shift
        rect = pymupdf.Rect(x, top - size * 1.1, x + width, top + size * 2.5)
        try:
            page.insert_textbox(rect, s, fontsize=size, fontname=font, color=(0, 0, 0))
        except Exception:
            page.insert_text((x, top), s, fontsize=size, fontname="helv")


def draw_template(page: pymupdf.Page, name: str) -> None:
    if not name or name.lower() == "blank":
        return
    for ext in (".svg", ".png"):
        f = TEMPLATE_DIR / f"{name}{ext}"
        if f.exists():
            try:
                if ext == ".svg":
                    with pymupdf.open(f) as svg:
                        pdf = pymupdf.open("pdf", svg.convert_to_pdf())
                    page.show_pdf_page(page.rect, pdf, 0, keep_proportion=False)
                else:
                    page.insert_image(page.rect, filename=str(f), keep_proportion=False)
            except Exception:
                pass
            return


def build_pdf(document: Document, *, pages: list[int] | None = None, annotations: bool = True,
              templates: bool = True) -> pymupdf.Document:
    """The document as a PDF with ink drawn in (vector)."""
    out = pymupdf.open()
    src = pymupdf.open(stream=document.pdf, filetype="pdf") if document.pdf else None
    try:
        wanted = document.pages if pages is None else [document.pages[i] for i in pages if 0 <= i < len(document.pages)]
        for pg in wanted:
            tree = read_scene(pg) if annotations else None
            if src is not None and pg.pdf_page is not None and pg.pdf_page < src.page_count:
                out.insert_pdf(src, from_page=pg.pdf_page, to_page=pg.pdf_page)
                page = out[-1]
                if page.rotation:
                    page.remove_rotation()
                if tree is not None:
                    draw_strokes(page, tree, x_shift=page.rect.width / 2)
            else:
                pw, ph = paper_size(tree) if tree is not None else _doc_paper(document)
                height_units = ph
                if tree is not None:
                    _, _, _, ymax = bounds(tree, anchor_positions(tree.root_text, ph))
                    height_units = max(ph, ymax + 40)
                page = out.new_page(width=pw * SCALE, height=height_units * SCALE)
                if templates:
                    tpl = page.rect if height_units == ph else pymupdf.Rect(0, 0, pw * SCALE, ph * SCALE)
                    if tpl == page.rect:
                        draw_template(page, pg.template)
                if tree is not None:
                    draw_strokes(page, tree, x_shift=pw * SCALE / 2)
    finally:
        if src is not None:
            src.close()
    return out


def _doc_paper(document: Document) -> tuple[int, int]:
    for pg in document.pages:
        tree = read_scene(pg)
        if tree is not None:
            return paper_size(tree)
    return (1620, 2160)


def page_cache_key(document: Document, page: Page) -> str:
    h = hashlib.sha1()
    h.update(page.rm or b"")
    h.update(str(page.pdf_page).encode())
    h.update(page.template.encode())
    h.update(b"r3")  # bump when drawing changes
    return h.hexdigest()[:16]


def page_png(document: Document, index: int, *, width: int = 1240) -> Path:
    """Render one page to PNG (cached by content) and return its path."""
    page = document.pages[index]
    key = page_cache_key(document, page)
    target = config.RENDER_CACHE / document.id / f"{index:04d}-{key}-{width}.png"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with build_pdf(document, pages=[index]) as pdf:
        p = pdf[0]
        zoom = width / p.rect.width
        pix = p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        tmp = target.with_suffix(".tmp.png")
        pix.save(tmp)
        tmp.replace(target)
    for old in target.parent.glob(f"{index:04d}-*-{width}.png"):
        if old != target:
            old.unlink(missing_ok=True)
    return target


def thumbnail(document: Document, *, width: int = 300) -> Path:
    """First page (or the cover page the tablet uses) as a small PNG."""
    cover = int(document.content.get("coverPageNumber", 0) or 0)
    index = cover if 0 <= cover < len(document.pages) else 0
    if cover == -1:  # "last opened page" on the tablet
        index = int(document.metadata.get("lastOpenedPage", 0) or 0)
        index = index if 0 <= index < len(document.pages) else 0
    if not document.pages:
        raise ValueError("document has no pages")
    return page_png(document, index, width=width)


def export_pdf(document: Document, target: Path, **kw) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with build_pdf(document, **kw) as pdf:
        pdf.set_metadata({"title": document.name, "creator": "omarchy-remarkable"})
        pdf.save(target, garbage=3, deflate=True)
    return target


def export_svg(document: Document, index: int, target: Path) -> Path:
    with build_pdf(document, pages=[index]) as pdf:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(pdf[0].get_svg_image(text_as_path=False))
    return target
