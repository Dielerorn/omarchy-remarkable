"""Templates: the .template renderer, fetch into the render cache, install/remove."""

from __future__ import annotations

import json

import pymupdf
import pytest

from rmbridge import cli, render
from rmbridge import tablet_templates as tt
from rmbridge.tablet_remote import TabletError

# Shapes taken from stock-derived .template files (chrissyhroberts/remarkable_2_and_paper_pro_custom_templates).
CHECKLIST = {
    "name": "Checklist", "author": "reMarkable", "templateVersion": "1.0.0", "formatVersion": 1,
    "categories": ["Planners"], "orientation": "landscape",
    "constants": [
        {"mobileMaxWidth": 1000}, {"boxSize": 55}, {"magicLineOffsetY": 149}, {"boxesOffsetY": 228},
        {"magicBoxOffsetX": 125}, {"mobileBoxOffsetX": 100},
        {"offsetX": "templateHeight > mobileMaxWidth ? magicBoxOffsetX : mobileBoxOffsetX"},
        {"lineOffsetX": 210},
    ],
    "items": [
        {"type": "text", "text": "Deadline", "fontSize": 24, "position": {"x": "templateWidth / 2 + 750", "y": 110}},
        {"type": "path", "data": ["M", 0, "magicLineOffsetY", "L", "templateWidth", "magicLineOffsetY"]},
        {"type": "group", "boundingBox": {"x": "lineOffsetX", "y": "boxesOffsetY", "width": "templateWidth",
                                          "height": 97.4},
         "repeat": {"rows": "down"},
         "children": [{"type": "path", "data": ["M", "-offsetX", 0, "L", "boxSize - offsetX", 0,
                                                "L", "boxSize - offsetX", "boxSize", "L", "-offsetX", "boxSize",
                                                "Z", "M", 0, "boxSize", "L", "templateWidth", "boxSize"]}]},
    ],
}
LINES = {
    "name": "Lines", "orientation": "portrait",
    "constants": [{"step": 80}],
    "items": [{"type": "group", "boundingBox": {"x": 0, "y": 200, "width": "templateWidth", "height": "step"},
               "repeat": {"rows": "down"},
               "children": [{"type": "path", "strokeWidth": 2, "data": ["M", 0, 0, "L", "templateWidth", 0]}]},
              {"type": "image", "src": "x.png"}],
}


def test_expressions():
    names = {"templateWidth": 1620, "templateHeight": 2160, "a": 10}
    ev = tt.evaluate
    assert ev("templateWidth / 2 + 30", names) == 840
    assert ev("-a * 2", names) == -20
    assert ev("templateHeight > 1000 ? a : 0", names) == 10
    assert ev("a > 5 && a < 20 ? 1 : 2", names) == 1
    assert ev("!(a == 10) || false ? 1 : (2 + 3) % 4", names) == 1
    assert ev("1 ? 2 ? 3 : 4 : 5", names) == 3
    assert ev(7.5, names) == 7.5
    with pytest.raises(tt.ExprError):
        ev("unknownName + 1", names)
    with pytest.raises(tt.ExprError):
        ev("__import__('os')", names)


def test_render_landscape_template_to_portrait_svg():
    svg, warnings = tt.template_to_svg(CHECKLIST)
    assert warnings == []
    assert 'width="1620" height="2160"' in svg and 'transform="translate(1620 0) rotate(90)"' in svg
    assert ">Deadline</text>" in svg
    # Rows repeat down a 1620-high landscape page: (1620 - 228) / 97.4 -> 15 rows.
    assert svg.count("<path") == 1 + 15
    with pymupdf.open(stream=svg.encode(), filetype="svg") as doc:
        assert (round(doc[0].rect.width), round(doc[0].rect.height)) == (1620, 2160)


def test_render_reports_unsupported_items():
    svg, warnings = tt.template_to_svg(LINES)
    assert svg.count("<path") == (2160 - 200) // 80 + 1
    assert warnings == ["image: unsupported item type 'image'"]


@pytest.fixture
def tablet(sandbox):
    tdir = sandbox.path("/usr/share/remarkable/templates")
    tdir.mkdir(parents=True)
    registry = {"templates": [
        {"name": "Blank", "filename": "Blank", "iconCode": "", "categories": ["Creative"]},
        {"name": "Blank", "filename": "Blank", "iconCode": "", "landscape": True, "categories": ["Creative"]},
        {"name": "Lined small", "filename": "P Lines small", "iconCode": "", "categories": ["Lines"]},
        {"name": "Dots S", "filename": "P Dots S", "iconCode": "", "categories": ["Grids"]},
        {"name": "Checklist", "filename": "LS Checklist", "iconCode": "", "landscape": True,
         "categories": ["Planners"]},
        {"name": "Ghost", "filename": "Not On Disk", "categories": ["Lines"]},
    ]}
    (tdir / "templates.json").write_text(json.dumps(registry, indent=2, ensure_ascii=False))
    (tdir / "Blank.png").write_bytes(b"\x89PNG fake")
    (tdir / "P Lines small.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1620" height="2160">'
        '<path d="M0 300 H1620" stroke="#000" stroke-width="3"/></svg>')
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 162, 216), False)
    pix.clear_with(250)
    pix.save(tdir / "P Dots S.png")
    (tdir / "LS Checklist.template").write_text(json.dumps(CHECKLIST))
    (tdir / "broken.template").write_text("{not json")
    sandbox.lock_rootfs()
    return sandbox


def run_cli(argv, capsys) -> dict:
    code = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return {"code": code, **json.loads(out[-1])}


def test_fetch_fills_render_cache(tablet, isolated, monkeypatch):
    from rmbridge import ssh
    result = ssh.fetch_templates()
    cache = isolated / "cache" / "templates"
    assert result["dir"] == str(cache) and result["firmware"] == "3.27.3.0"
    assert (cache / "P Lines small.svg").exists() and (cache / "P Dots S.png").exists()
    assert (cache / "LS Checklist.svg").exists() and result["rendered"] == ["LS Checklist.svg"]
    assert result["missing"] == ["Not On Disk"]
    assert result["skipped"][0]["file"] == "broken.template"
    index = json.loads((cache / "index.json").read_text())
    assert {t["filename"] for t in index["templates"]} >= {"P Lines small", "LS Checklist"}

    # render.draw_template finds the files by the page's template value.
    monkeypatch.setattr(render, "TEMPLATE_DIR", cache)
    for name in ("P Lines small", "LS Checklist", "P Dots S"):
        doc = pymupdf.open()
        page = doc.new_page(width=1620 * render.SCALE, height=2160 * render.SCALE)
        render.draw_template(page, name)
        assert page.get_contents() or page.get_images(), name
        doc.close()

    # A second fetch replaces the snapshot.
    (tablet.path("/usr/share/remarkable/templates") / "P Dots S.png").chmod(0o644)
    tablet.unlock_rootfs()
    (tablet.path("/usr/share/remarkable/templates") / "P Dots S.png").unlink()
    ssh.fetch_templates()
    assert not (cache / "P Dots S.png").exists()


def test_list_marks_custom_only_after_backup(tablet, capsys):
    result = run_cli(["ssh", "templates", "list"], capsys)
    assert result["ok"] and result["cached"] is False and result["stockKnown"] is False
    lines = next(t for t in result["templates"] if t["filename"] == "P Lines small")
    assert lines["files"] == ["P Lines small.svg"] and lines["custom"] is False


def test_install_needs_yes_and_checks_category(tablet, tmp_path, capsys):
    svg = tmp_path / "tracker.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 810 1080"><rect width="10" height="10"/></svg>')
    result = run_cli(["ssh", "templates", "install", str(svg), "--name", "exercise tracker"], capsys)
    assert result["code"] == "needs-confirmation" and any("scaled the SVG" in s for s in result["plan"])
    bad = run_cli(["ssh", "templates", "install", str(svg), "--name", "x", "--category", "Fun", "--yes"], capsys)
    assert bad["ok"] is False and "Creative, Grids, Lines, Planners" in bad["error"]
    assert tablet.calls() == []


def test_install_and_remove_custom_template(tablet, tmp_path, capsys, isolated):
    tdir = tablet.path("/usr/share/remarkable/templates")
    stock_registry = (tdir / "templates.json").read_text()
    svg = tmp_path / "tracker.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 810 1080"><rect width="10" height="10"/></svg>')
    result = run_cli(["ssh", "templates", "install", str(svg), "--name", "exercise tracker",
                      "--category", "Planners", "--yes"], capsys)
    assert result["ok"], result
    assert result["files"] == ["exercise tracker.png", "exercise tracker.svg"]
    assert result["restarted"] and "systemctl restart xochitl" in tablet.calls()
    assert "firmware update" in result["warning"]
    assert tablet.calls()[:2] == ["mount -o remount,rw /", "mount -o remount,ro /"]

    registry = json.loads((tdir / "templates.json").read_text())
    names = [e["filename"] for e in registry["templates"]]
    assert names.index("exercise tracker") == 2  # right after the two Blank entries
    entry = registry["templates"][2]
    assert entry["categories"] == ["Planners"] and entry["iconCode"] == "" and "landscape" not in entry
    assert 'width="1620" height="2160"' in (tdir / "exercise tracker.svg").read_text()
    assert (tdir / "exercise tracker.png").read_bytes()[:4] == b"\x89PNG"
    # Stock registry kept on the tablet and here; sources kept on /home for after an update.
    assert tablet.path("/home/root/.omarchy-remarkable-backup/3.27.3.0/usr/share/remarkable/templates/templates.json").read_text() == stock_registry
    assert (isolated / "data/tablet-backup/3.27.3.0/usr/share/remarkable/templates/templates.json").exists()
    assert tablet.path("/home/root/.omarchy-remarkable-backup/custom-templates/exercise tracker.svg").exists()
    # And the render cache has it straight away.
    assert (isolated / "cache/templates/exercise tracker.svg").exists()

    listed = run_cli(["ssh", "templates", "list"], capsys)
    custom = [t["filename"] for t in listed["templates"] if t["custom"]]
    assert custom == ["exercise tracker"]

    # Stock templates can't be removed or shadowed.
    refused = run_cli(["ssh", "templates", "remove", "Lined small", "--yes"], capsys)
    assert refused["ok"] is False and "came with the tablet" in refused["error"]
    shadow = run_cli(["ssh", "templates", "install", str(svg), "--name", "P Lines small", "--yes"], capsys)
    assert shadow["ok"] is False and "tablet's own" in shadow["error"]

    removed = run_cli(["ssh", "templates", "remove", "exercise tracker", "--yes", "--no-restart"], capsys)
    assert removed["ok"] and removed["removed"] == ["exercise tracker"] and removed["restarted"] is False
    assert not (tdir / "exercise tracker.svg").exists() and not (tdir / "exercise tracker.png").exists()
    assert json.loads((tdir / "templates.json").read_text()) == json.loads(stock_registry)
    assert not tablet.path("/home/root/.omarchy-remarkable-backup/custom-templates/exercise tracker.svg").exists()


def test_install_landscape_png_is_resized(tablet, tmp_path):
    png = tmp_path / "wide.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 300), False)
    pix.clear_with(10)
    pix.save(png)
    prepared = tt.prepare_install(png, "Wide one", "Grids", None)
    assert prepared["entry"]["landscape"] is True
    from rmbridge.tablet_system import png_size
    assert png_size(prepared["files"]["Wide one.png"]) == (2160, 1620)
    tt.install(tablet, prepared, restart=False)
    assert tablet.path("/usr/share/remarkable/templates/Wide one.png").exists()


def test_remove_refused_when_nothing_was_installed(tablet):
    with pytest.raises(TabletError, match="came with the tablet"):
        tt.remove(tablet, "Dots S", restart=False)
    with pytest.raises(TabletError, match="No template"):
        tt.remove(tablet, "nope", restart=False)


def test_safe_filename():
    assert tt.safe_filename("  exercise   tracker ") == "exercise tracker"
    assert tt.safe_filename("a/b\\c:d") == "abcd"
    with pytest.raises(ValueError):
        tt.safe_filename("///")
