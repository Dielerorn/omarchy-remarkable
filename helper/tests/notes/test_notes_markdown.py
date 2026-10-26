"""notes.py: typed text, highlights, and the Markdown notes it writes."""

from __future__ import annotations

from rmscene import scene_items as si

from rmbridge import notes, render, transcribe
from rmbridge.doc import Page
from rmfixtures import glyph, make_pdf, make_rmdoc, page_rm, stroke

PS = si.ParagraphStyle
YELLOW = (255, 237, 117, 255)  # the Paper Pro's own highlighter colours
GREEN = (172, 255, 133, 255)
ORANGE = (255, 195, 140, 255)


def tree_of(rm: bytes):
    return render.read_scene(Page(index=0, id="p", rm=rm))


# ------------------------------------------------------------ typed text


def test_typed_text_styles_become_markdown():
    rm = page_rm([
        (PS.HEADING, [("Weekly plan", False)]),
        (PS.BOLD, [("Before Friday", False)]),
        (PS.PLAIN, [("Call ", False), ("mum", True), (" today", False)]),
        (PS.PLAIN, [("", False)]),
        (PS.BULLET, [("milk", False)]),
        (PS.BULLET2, [("oat", False)]),
        (PS.CHECKBOX, [("bread", False)]),
        (PS.CHECKBOX_CHECKED, [("eggs", False)]),
        (PS.PLAIN, [("   centred text   ", False)]),
    ])
    assert notes.typed_markdown(tree_of(rm)) == (
        "### Weekly plan\n\n**Before Friday**\n\nCall **mum** today\n\n"
        "- milk\n  - oat\n- [ ] bread\n- [x] eggs\n\ncentred text"
    )
    assert notes.typed_plain(tree_of(rm)).splitlines()[0] == "Weekly plan"


def test_typed_text_keeps_scripts_and_soft_breaks():
    rm = page_rm([(PS.PLAIN, [("Привет Hej då 안녕하세요", False)])])
    assert notes.typed_markdown(tree_of(rm)) == "Привет\nHej då\n안녕하세요"


def test_page_without_text_has_no_typed_markdown():
    assert notes.typed_markdown(tree_of(page_rm(lines=[stroke()]))) == ""
    assert notes.typed_markdown(None) == ""


# ------------------------------------------------------------ highlights


def test_color_names_for_tablet_colours():
    assert notes.color_name(YELLOW) == "yellow"
    assert notes.color_name(GREEN) == "green"
    assert notes.color_name(ORANGE) == "orange"
    assert notes.color_name((255, 105, 180, 255)) == "pink"
    assert notes.color_name((78, 105, 201, 255)) == "blue"
    assert notes.color_name((200, 200, 200, 255)) == "gray"


def test_multiline_highlight_is_one_passage():
    # How the tablet stores one highlight across three lines, plus a repeat.
    rm = page_rm(glyphs=[
        glyph("Now there were giants on the earth,", 100),
        glyph("and also afterward, when the sons", 137),
        glyph("and also afterward, when the sons", 137),
        glyph("of God came in.", 172),
        glyph("God", 175),  # inside the previous range
        glyph("separate passage", 400),
        glyph("touching but green", 418, GREEN),
    ])
    found = notes.page_highlights(tree_of(rm), 4)
    assert [(h.text, h.color) for h in found] == [
        ("Now there were giants on the earth, and also afterward, when the sons of God came in.", "yellow"),
        ("separate passage", "yellow"),
        ("touching but green", "green"),
    ]
    assert found[0].page == 4
    assert found[2].hex == "#acff85"


def test_highlights_without_offsets_keep_reading_order():
    rm = page_rm(glyphs=[glyph("second", None, y=900), glyph("first", None, y=100), glyph("first", None, y=100)])
    assert [h.text for h in notes.page_highlights(tree_of(rm), 0)] == ["first", "second"]


def test_highlight_whitespace_is_normalised_and_empty_ones_dropped():
    rm = page_rm(glyphs=[glyph("Sep\t01   Gen\t1-3", 5), glyph("   ", 40)])
    assert [h.text for h in notes.page_highlights(tree_of(rm), 0)] == ["Sep 01 Gen 1-3"]


# --------------------------------------------------------------- extract


def test_extract_flags_ink_but_not_highlighter(env, tmp_path):
    rmdoc = make_rmdoc(tmp_path / "a.rmdoc", "aaaaaaaa-0000-4000-8000-000000000001", "Mixed", [
        page_rm(lines=[stroke()]),
        page_rm(lines=[stroke(tool=si.Pen.HIGHLIGHTER_2, color=si.PenColor.YELLOW)], glyphs=[glyph("quote", 1)]),
        None,
    ])
    env.add("aaaaaaaa-0000-4000-8000-000000000001", "/Mixed", rmdoc)
    _e, doc = env.open_document("aaaaaaaa-0000-4000-8000-000000000001")
    pages = notes.extract(doc)
    assert [(p.index, p.ink, bool(p.highlights)) for p in pages] == [(0, True, False), (1, False, True)]
    assert transcribe.page_has_writing(doc.pages[0]) and not transcribe.page_has_writing(doc.pages[1])


# ----------------------------------------------------------------- notes

DOC = "bbbbbbbb-0000-4000-8000-000000000002"


def _notebook(env, tmp_path, path="/Courses/Russian/Lesson Notes", doc_id=DOC, tags=None):
    rmdoc = make_rmdoc(tmp_path / f"{doc_id}.rmdoc", doc_id, path.rsplit("/", 1)[-1], [
        page_rm([(PS.HEADING, [("Падежи", False)]), (PS.BULLET, [("родительный", False)])]),
        None,  # blank page: nothing to say
        page_rm(lines=[stroke()]),
    ], tags=tags)
    return env.add(doc_id, path, rmdoc)


def test_sync_writes_note_with_frontmatter_pages_and_images(env, tmp_path):
    _notebook(env, tmp_path, tags=["study", "Russian grammar"])
    path = notes.sync_document(DOC, settings=env.settings)
    assert path == env.notes_dir / "Courses" / "Russian" / "Lesson Notes.md"
    text = path.read_text()
    assert text.startswith("---\ntitle: \"Lesson Notes\"\nsource: reMarkable\n")
    assert f"remarkable_id: {DOC}\n" in text
    assert 'path: "/Courses/Russian/Lesson Notes"' in text
    assert "type: notebook\npages: 3\n" in text
    assert "tags:\n  - remarkable\n  - study\n  - Russian-grammar\n---" in text
    assert "## Page 1\n\n### Падежи\n\n- родительный" in text
    assert "## Page 2" not in text  # blank page skipped
    assert "## Page 3\n\n![[Lesson Notes bbbbbbbb p3.png]]" in text
    assert text.endswith(notes.MARKER + "\n")
    # Only the page with ink gets an image.
    assert sorted(p.name for p in (env.notes_dir / "attachments").iterdir()) == ["Lesson Notes bbbbbbbb p3.png"]


def test_sync_includes_cached_transcript(env, tmp_path):
    _notebook(env, tmp_path)
    _e, doc = env.open_document(DOC)
    transcribe._store(doc, doc.pages[2], "# Причастия\nЯ **люблю** грамматику\n- пункт")
    text = notes.sync_document(DOC, settings=env.settings).read_text()
    # Transcript headings sit below the page heading; #tags stay tags.
    assert "## Page 3\n\n### Причастия\nЯ **люблю** грамматику\n- пункт\n\n![[" in text
    assert notes.demote_headings("#tag\n```\n# code\n```") == "#tag\n```\n# code\n```"


def test_user_notes_survive_resync_and_unchanged_note_is_not_rewritten(env, tmp_path):
    _notebook(env, tmp_path)
    path = notes.sync_document(DOC, settings=env.settings)
    path.write_text(path.read_text() + "My own thoughts.\n\n- [ ] review\n")
    first = path.read_text()
    assert notes.sync_document(DOC, settings=env.settings) == path
    assert path.read_text() == first
    assert path.read_text().endswith(notes.MARKER + "\nMy own thoughts.\n\n- [ ] review\n")


def test_note_without_marker_keeps_its_body(env, tmp_path):
    _notebook(env, tmp_path)
    path = notes.sync_document(DOC, settings=env.settings)
    head, _ = path.read_text().split(notes.MARKER)
    path.write_text(head.rstrip() + "\n\nedited by hand\n")
    text = notes.sync_document(DOC, settings=env.settings).read_text()
    assert text.split(notes.MARKER, 1)[1].strip().endswith("edited by hand")


def test_images_off(env, tmp_path):
    _notebook(env, tmp_path)
    env.settings["notesIncludeImages"] = False
    text = notes.sync_document(DOC, settings=env.settings).read_text()
    assert "![[" not in text and "## Page 3" not in text
    assert not (env.notes_dir / "attachments").exists()


def test_moved_document_takes_its_note_along(env, tmp_path):
    _notebook(env, tmp_path)
    old = notes.sync_document(DOC, settings=env.settings)
    old.write_text(old.read_text() + "keep me\n")
    _notebook(env, tmp_path, path="/Archive/Lesson Notes (old)")
    new = notes.sync_document(DOC, settings=env.settings)
    assert new == env.notes_dir / "Archive" / "Lesson Notes (old).md"
    assert not old.exists()
    assert new.read_text().endswith("keep me\n")


def test_same_name_twice_and_foreign_files_get_their_own_note(env, tmp_path):
    other = "cccccccc-0000-4000-8000-000000000003"
    (env.notes_dir / "Journal").mkdir(parents=True)
    (env.notes_dir / "Journal" / "Lesson Notes.md").write_text("my own file\n")
    _notebook(env, tmp_path, path="/Journal/Lesson Notes")
    _notebook(env, tmp_path, path="/Journal/Lesson Notes", doc_id=other)
    a = notes.sync_document(DOC, settings=env.settings)
    b = notes.sync_document(other, settings=env.settings)
    assert a.name == "Lesson Notes (bbbbbbbb).md" and b.name == "Lesson Notes (cccccccc).md"
    assert (env.notes_dir / "Journal" / "Lesson Notes.md").read_text() == "my own file\n"


def test_nothing_to_say(env, tmp_path):
    rmdoc = make_rmdoc(tmp_path / "e.rmdoc", "dddddddd-0000-4000-8000-000000000004", "Empty", [None, None])
    env.add("dddddddd-0000-4000-8000-000000000004", "/Empty", rmdoc)
    assert notes.sync_document("dddddddd-0000-4000-8000-000000000004", settings=env.settings) is None
    forced = notes.sync_document("dddddddd-0000-4000-8000-000000000004", settings=env.settings, always=True)
    assert forced.exists() and "## Page" not in forced.read_text()


def test_pdf_highlights_in_note_and_command(env, tmp_path):
    doc_id = "eeeeeeee-0000-4000-8000-000000000005"
    rmdoc = make_rmdoc(tmp_path / "b.rmdoc", doc_id, "A Book", [
        None, page_rm(glyphs=[glyph("first line", 0), glyph("second line", 12)]),
    ], file_type="pdf", pdf=make_pdf(["Title page", "first line second line"]))
    env.add(doc_id, "/Books/A Book", rmdoc)
    text = notes.sync_document(doc_id, settings=env.settings).read_text()
    assert "## Page 2\n\n> first line second line\n> #highlight/yellow" in text
    assert "attachments" not in text

    import argparse
    out = notes.cmd_highlights(argparse.Namespace(id=doc_id, format="json"))
    assert out["ok"] and out["pages"] == [{"index": 1, "typed": "", "handwriting": "", "highlights": [
        {"text": "first line second line", "color": "yellow", "hex": "#ffed75"}]}]
    md = notes.cmd_highlights(argparse.Namespace(id=doc_id, format="md"))["markdown"]
    assert md == "# A Book\n\n## Page 2\n\n> first line second line\n> #highlight/yellow\n"


def test_sync_changed_only_touches_noted_or_auto_documents(env, tmp_path):
    noted = _notebook(env, tmp_path)
    auto_id = "ffffffff-0000-4000-8000-000000000006"
    other_id = "99999999-0000-4000-8000-000000000007"
    _notebook(env, tmp_path, path="/Auto/Diary", doc_id=auto_id)
    _notebook(env, tmp_path, path="/Elsewhere/Thing", doc_id=other_id)
    notes.sync_document(DOC, settings=env.settings)
    env.settings["autoTranscribeFolders"] = ["/Auto"]
    state: dict = {}

    written = notes.sync_changed(env.library(), state)
    assert sorted(p.name for p in written) == ["Diary.md", "Lesson Notes.md"]
    assert set(state["notesSynced"]) == {DOC, auto_id}

    env.opened.clear()
    assert notes.sync_changed(env.library(), state) == []
    assert env.opened == []  # nothing changed: nothing opened

    noted.modified = "2026-02-02T00:00:00Z"
    assert [p.name for p in notes.sync_changed(env.library(), state)] == ["Lesson Notes.md"]
    assert state["notesSynced"][DOC] == "2026-02-02T00:00:00Z"


def test_sync_changed_picks_up_new_transcripts(env, tmp_path):
    _notebook(env, tmp_path)
    note = notes.sync_document(DOC, settings=env.settings)
    state = {"notesSynced": {DOC: "2026-01-01T00:00:00Z"}}
    import os
    os.utime(note, (1, 1))  # note older than the transcript below
    _e, doc = env.open_document(DOC)
    transcribe._store(doc, doc.pages[2], "new words")
    assert notes.sync_changed(env.library(), state) == [note]
    assert "new words" in note.read_text()
