"""Build small synthetic .rm pages and .rmdoc files for tests.

Real v6 scene files written with rmscene's own writer, so tests exercise the
same parsing path as tablet data without shipping anyone's notebooks.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from uuid import UUID

from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequence, CrdtSequenceItem
from rmscene.scene_stream import (AuthorIdsBlock, MigrationInfoBlock, PageInfoBlock, RootTextBlock,
                                  SceneGlyphItemBlock, SceneGroupItemBlock, SceneLineItemBlock, SceneTreeBlock,
                                  TreeNodeBlock, write_blocks)
from rmscene.tagged_block_common import CrdtId, LwwValue

AUTHOR = UUID("00000000-0000-4000-8000-000000000001")
LAYER = CrdtId(0, 11)

# A paragraph: (style, [(text, bold)]) -- inline bold uses the tablet's format codes.
Paragraph = tuple[si.ParagraphStyle, list[tuple[str, bool]]]


def _text_block(paragraphs: list[Paragraph]) -> RootTextBlock:
    items: list[CrdtSequenceItem] = []
    styles: dict = {}
    next_id = 16
    left = CrdtId(0, 0)

    def add(value) -> CrdtId:
        nonlocal next_id, left
        item_id = CrdtId(1, next_id)
        items.append(CrdtSequenceItem(item_id, left, CrdtId(0, 0), 0, value))
        length = len(value) if isinstance(value, str) else 1
        last = CrdtId(1, next_id + length - 1)
        next_id += length
        left = last
        return item_id

    for n, (style, spans) in enumerate(paragraphs):
        if n == 0:
            styles[CrdtId(0, 0)] = LwwValue(CrdtId(1, 1), style)
        else:
            newline = add("\n")
            styles[newline] = LwwValue(CrdtId(1, 2 + n), style)
        for text, bold in spans:
            if bold:
                add(1)
            add(text)
            if bold:
                add(2)
    text = si.Text(items=CrdtSequence(items), styles=styles, pos_x=-468.0, pos_y=234.0, width=936.0)
    return RootTextBlock(block_id=CrdtId(0, 0), value=text)


def stroke(x: float = -300, y: float = 600, n: int = 20, tool: si.Pen = si.Pen.FINELINER_2,
           color: si.PenColor = si.PenColor.BLACK) -> si.Line:
    points = [si.Point(x + i * 12, y + (i % 3) * 8, 10, 0, 8, 200) for i in range(n)]
    return si.Line(color=color, tool=tool, points=points, thickness_scale=2.0, starting_length=0.0)


def glyph(text: str, start: int | None, rgba=(255, 237, 117, 255), y: float = 400.0) -> si.GlyphRange:
    return si.GlyphRange(start=start, length=len(text), text=text, color=si.PenColor.HIGHLIGHT,
                         rectangles=[si.Rectangle(-500.0, y, 600.0, 40.0)], color_rgba=rgba)


def page_rm(paragraphs: list[Paragraph] | None = None, lines: list[si.Line] | None = None,
            glyphs: list[si.GlyphRange] | None = None) -> bytes:
    blocks: list = [
        AuthorIdsBlock(author_uuids={1: AUTHOR}),
        MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
        SceneTreeBlock(tree_id=LAYER, node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1)),
    ]
    if paragraphs:
        blocks.append(_text_block(paragraphs))
    blocks += [
        TreeNodeBlock(si.Group(node_id=CrdtId(0, 1))),
        TreeNodeBlock(si.Group(node_id=LAYER, label=LwwValue(CrdtId(0, 12), "Layer 1"))),
        SceneGroupItemBlock(parent_id=CrdtId(0, 1),
                            item=CrdtSequenceItem(CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, LAYER)),
    ]
    left = CrdtId(0, 0)
    serial = 100
    for value in (lines or []) + (glyphs or []):
        item_id = CrdtId(2, serial)
        block = SceneLineItemBlock if isinstance(value, si.Line) else SceneGlyphItemBlock
        blocks.append(block(parent_id=LAYER, item=CrdtSequenceItem(item_id, left, CrdtId(0, 0), 0, value)))
        left = item_id
        serial += 1
    buf = io.BytesIO()
    write_blocks(buf, blocks, options={"version": "3.1"})
    return buf.getvalue()


def make_pdf(texts: list[str]) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for t in texts:
        page = doc.new_page(width=420, height=595)
        page.insert_textbox(pymupdf.Rect(40, 40, 380, 560), t, fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def make_rmdoc(target: Path, doc_id: str, name: str, pages: list[bytes | None], *, file_type: str = "notebook",
               pdf: bytes | None = None, tags: list[str] | None = None) -> Path:
    """An .rmdoc as the cloud hands it out: .content, .metadata, page .rm files, optional PDF."""
    page_ids = [f"{doc_id[:8]}-page-{i:04d}" for i in range(len(pages))]
    cpages = []
    for i, pid in enumerate(page_ids):
        entry = {"id": pid, "idx": {"timestamp": "1:2", "value": f"b{chr(97 + i // 26)}{chr(97 + i % 26)}"},
                 "template": {"timestamp": "1:1", "value": "Blank"}}
        if pdf is not None:
            entry["redir"] = {"timestamp": "1:2", "value": i}
        cpages.append(entry)
    content = {"cPages": {"pages": cpages}, "fileType": file_type, "formatVersion": 2,
               "tags": [{"name": t, "timestamp": 1} for t in tags] if tags else None}
    metadata = {"visibleName": name, "type": "DocumentType", "parent": ""}
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as z:
        z.writestr(f"{doc_id}.content", json.dumps(content))
        z.writestr(f"{doc_id}.metadata", json.dumps(metadata))
        if pdf is not None:
            z.writestr(f"{doc_id}.pdf", pdf)
        for pid, rm in zip(page_ids, pages):
            if rm is not None:
                z.writestr(f"{doc_id}/{pid}.rm", rm)
    return target
