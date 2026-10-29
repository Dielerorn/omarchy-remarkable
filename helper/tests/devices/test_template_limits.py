"""A small .template can't make the helper render without bound: repeats,
nesting and output share one budget, checked before the --yes prompt."""

from __future__ import annotations

import json
import struct
import time
import tracemalloc
import zlib

import pytest

from rmbridge import tablet_system as ts
from rmbridge import tablet_templates as tt


def _group(children, rows=2000, cols=2000):
    return {"type": "group", "boundingBox": {"x": 0, "y": 0, "width": 1, "height": 1},
            "repeat": {"rows": rows, "columns": cols}, "children": children}


LINE = {"type": "path", "data": ["M", 0, 0, "L", 10, 10]}


@pytest.mark.parametrize("template", [
    {"items": [_group([_group([_group([LINE])])])]},  # 2000^6 lines in a few hundred bytes
    {"items": [_group([_group([])])]},  # empty tiles still cost time
    {"items": [_group([{"type": "text", "text": "x" * 100_000, "position": {"x": 0, "y": 0}}], 10, 10)]},  # 10 MB of text
])
def test_expansion_stops_at_the_budget(template):
    tracemalloc.start()
    t = time.monotonic()
    with pytest.raises(tt.TemplateTooLarge):
        tt.template_to_svg(template)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert time.monotonic() - t < 10
    assert peak < 64 * 1024 * 1024


def test_nesting_depth_is_limited():
    item = LINE
    for _ in range(40):
        item = _group([item], 1, 1)
    with pytest.raises(tt.TemplateTooLarge, match="nested"):
        tt.template_to_svg({"items": [item]})


def test_budget_errors_are_not_downgraded_to_warnings():
    # _render_item turns ValueErrors into warnings; TemplateTooLarge must get through.
    with pytest.raises(tt.TemplateTooLarge):
        tt.template_to_svg({"items": [{"type": "unknown"}, _group([_group([LINE])])]})


def test_deep_expressions_are_a_warning_not_a_crash():
    deep = "(" * 5000 + "1" + ")" * 5000
    svg, warnings = tt.template_to_svg({"items": [{"type": "path", "data": ["M", deep, 0, "L", 1, 1]}]})
    assert any("nested too deeply" in w for w in warnings)


def test_ordinary_templates_still_render():
    lined = {"items": [{"type": "group", "boundingBox": {"x": 0, "y": 0, "width": "templateWidth", "height": 60},
                        "repeat": {"rows": "down"}, "children": [{"type": "path", "data": ["M", 0, 59, "L", "templateWidth", 59]}]}]}
    svg, warnings = tt.template_to_svg(lined)
    assert svg.count("<path") > 20 and not warnings


def test_install_refuses_before_confirming(tmp_path):
    bomb = tmp_path / "bomb.template"
    bomb.write_text(json.dumps({"items": [_group([_group([_group([LINE])])])]}))
    with pytest.raises(ValueError, match="repeats too much"):
        tt.prepare_install(bomb, "Bomb", "Creative", None)
    big = tmp_path / "big.template"
    big.write_text(" " * (tt.MAX_TEMPLATE_BYTES + 1) + "{}")
    with pytest.raises(ValueError, match="larger than"):
        tt.prepare_install(big, "Big", "Creative", None)


def test_imported_images_are_size_checked(tmp_path):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    png = tmp_path / "huge.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 100000, 100000, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b""))
    t = time.monotonic()
    with pytest.raises(ValueError, match="megapixels"):
        ts.fit_png(png)
    assert time.monotonic() - t < 1
