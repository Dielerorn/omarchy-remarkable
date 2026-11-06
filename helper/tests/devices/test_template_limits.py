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
    deep = "(" * 2000 + "1" + ")" * 2000
    svg, warnings = tt.template_to_svg({"items": [{"type": "path", "data": ["M", deep, 0, "L", 1, 1]}]})
    assert any("nested too deeply" in w for w in warnings)
    long = "1" + "+1" * tt.MAX_EXPR_CHARS
    svg, warnings = tt.template_to_svg({"items": [{"type": "path", "data": ["M", long, 0, "L", 1, 1]}]})
    assert any("longer than" in w for w in warnings)


def _bounded(fn, seconds=10, memory=64 * 1024 * 1024):
    """Run fn twice: once timed (under `seconds`), once traced (peak under
    `memory`; tracing makes it several times slower). Returns or raises what fn does."""
    t = time.monotonic()
    try:
        fn()
    except tt.TemplateTooLarge:
        pass
    assert time.monotonic() - t < seconds
    tracemalloc.start()
    try:
        return fn()
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert peak < memory


@pytest.mark.parametrize("constants", [
    # Each constant squares the last: 40 of them were 2^40 digits as Python ints.
    [{"c0": "999999999 * 999999999"}] + [{f"c{i}": f"c{i - 1} * c{i - 1}"} for i in range(1, 40)],
    [{"c0": 10 ** 4000}] + [{f"c{i}": f"c{i - 1} * c{i - 1}"} for i in range(1, 40)],  # a JSON integer
    [{"a": "9" * 2000 + " * " + "9" * 2000}],
    [{"a": "1e999"}, {"b": float("inf")}, {"c": float("nan")}, {"d": "1e308 * 10 - 1e308 * 10"}],
])
def test_constants_are_finite_doubles(constants):
    svg, warnings = _bounded(lambda: tt.template_to_svg({"constants": constants, "items": [LINE]}))
    assert "<path" in svg
    assert any("number too large" in w for w in warnings)


def test_constants_from_a_file_cannot_grow(tmp_path):
    # The case from the review: through the CLI's install path, before --yes.
    consts = [{"c0": "999999999 * 999999999"}] + [{f"c{i}": f"c{i - 1} * c{i - 1}"} for i in range(1, 64)]
    src = tmp_path / "grow.template"
    src.write_text(json.dumps({"constants": consts, "items": [LINE]}))
    prepared = _bounded(lambda: tt.prepare_install(src, "Grow", "Creative", None))
    assert any("number too large" in n for n in prepared["notes"])


def test_expressions_share_the_budget():
    # Many distinct long expressions: charged token by token, from the first constant on.
    long = "+".join(["1"] * 2000)
    constants = [{f"c{i}": long + f"+{i}"} for i in range(tt.MAX_EVAL_WORK // 2000 + 10)]
    with pytest.raises(tt.TemplateTooLarge, match="expressions"):
        _bounded(lambda: tt.template_to_svg({"constants": constants, "items": []}), memory=256 * 1024 * 1024)


def test_repeated_expressions_are_worked_out_once():
    # One long expression in 90,000 tiles: evaluated once, not 90,000 times.
    long = "+".join(["1"] * 2000)
    path = {"type": "path", "data": ["M", long, 0, "L", long, 1]}
    svg, warnings = _bounded(lambda: tt.template_to_svg({"items": [_group([path], 300, 300)]}), seconds=20,
                             memory=128 * 1024 * 1024)
    assert svg.count("<path") == 90_000 and not warnings


def test_warnings_are_few_and_short():
    item = {"type": "unknown", "id": "x" * 1_000_000}
    bad = {"type": "path", "data": ["M", "nope " * 500, 0]}
    svg, warnings = _bounded(lambda: tt.template_to_svg({"items": [_group([item, bad], 200, 200)]}))
    assert len(warnings) == tt.MAX_WARNINGS + 1 and warnings[-1].startswith("and ")
    assert all(len(w) <= tt.MAX_WARNING_CHARS for w in warnings)


def test_odd_shapes_are_warnings_not_crashes():
    items = [
        {"type": "group", "boundingBox": [1, 2], "repeat": "down", "children": {"a": 1}},
        {"type": "group", "repeat": {"rows": float("inf"), "columns": [3]}, "children": [LINE]},
        {"type": "path", "data": {"M": [0, 0]}},
        {"type": "path", "data": ["MOVE", 0, 0]},
        {"type": "text", "position": "top", "text": ["a", "b"], "fontSize": float("nan")},
        {"type": ["group"]},
    ]
    svg, warnings = tt.template_to_svg({"orientation": ["landscape"], "constants": {"a": 1}, "items": items})
    assert svg.startswith("<svg")
    with pytest.raises(ValueError, match="nested too deeply"):
        tt.parse_template(b"[" * 100_000, "deep.template")


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


def _huge_png(path):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 100000, 100000, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b""))
    return path


def test_imported_images_are_size_checked(tmp_path):
    png = _huge_png(tmp_path / "huge.png")
    t = time.monotonic()
    with pytest.raises(ValueError, match="megapixels"):
        ts.fit_png(png)
    assert time.monotonic() - t < 1
    # A PNG template too: its orientation comes from the header, not a decode.
    t = time.monotonic()
    with pytest.raises(ValueError, match="megapixels"):
        tt.prepare_install(png, "Huge", "Creative", None)
    assert time.monotonic() - t < 1


def _use_bomb(path, levels=9):
    """A 2 KB SVG that draws 10^levels squares through nested <use>."""
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
             'width="100" height="100"><defs><g id="a0"><rect width="1" height="1"/></g>']
    for i in range(1, levels + 1):
        parts.append(f'<g id="a{i}">' + f'<use xlink:href="#a{i - 1}"/>' * 10 + "</g>")
    parts.append(f'</defs><use xlink:href="#a{levels}"/></svg>')
    path.write_text("".join(parts))
    return path


def test_images_are_drawn_in_the_limited_process(tmp_path, monkeypatch):
    from rmbridge import web
    monkeypatch.setattr(web, "DECODE_TIMEOUT", 3)
    bomb = _use_bomb(tmp_path / "bomb.svg")
    t = time.monotonic()
    with pytest.raises(ValueError, match="memory or time"):
        ts.fit_png(bomb)
    with pytest.raises(ValueError, match="memory or time"):
        tt.prepare_install(bomb, "Bomb", "Creative", None)
    assert time.monotonic() - t < 15
