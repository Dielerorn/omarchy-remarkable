"""search.py: building the FTS index and querying it."""

from __future__ import annotations

import argparse
import sqlite3

import pytest
from rmscene import scene_items as si

from rmbridge import search, transcribe
from rmfixtures import glyph, make_pdf, make_rmdoc, page_rm, stroke

PS = si.ParagraphStyle
BOOK = "b0000000-0000-4000-8000-000000000001"
NOTE = "a0000000-0000-4000-8000-000000000002"
OTHER = "c0000000-0000-4000-8000-000000000003"


def _library(env, tmp_path):
    book = make_rmdoc(tmp_path / "book.rmdoc", BOOK, "Woodworking Basics", [
        None, page_rm(glyphs=[glyph("cut the dovetail joint", 0)]), None,
    ], file_type="pdf", pdf=make_pdf([
        "Chapter one. Choosing timber.",
        "To cut the dovetail joint, mark the tails first.",
        "Lesson two: rails and stiles.",
    ]))
    note = make_rmdoc(tmp_path / "note.rmdoc", NOTE, "Lesson Notes", [
        page_rm([(PS.HEADING, [("Падежи", False)]), (PS.PLAIN, [("родительный падеж", False)])]),
        page_rm(lines=[stroke()]),
    ])
    other = make_rmdoc(tmp_path / "other.rmdoc", OTHER, "Shopping", [
        page_rm([(PS.PLAIN, [("학교에서 milk and bread", False)])]),
    ])
    env.add(BOOK, "/Books/Woodworking Basics", book)
    env.add(NOTE, "/Courses/Russian/Lesson Notes", note)
    env.add(OTHER, "/Shopping", other)
    return env


def test_build_query_is_always_valid_fts():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='unicode61 remove_diacritics 2')")
    con.execute("INSERT INTO t VALUES ('the quick brown fox'), ('Привет мир'), ('학교에서 공부')")
    nasty = ['"unbalanced', 'AND', 'OR NOT', '*', '-', '--x', 'NEAR(a b)', 'text:fox', '^fox', '(', ')',
             'a"b"c', '{x}', '+x', "fox'", "😀", 'a AND', '"" ""', '- -', 'quick NOT', ':', '"NEAR"']
    for raw in nasty:
        query = search.build_query(raw)
        if query is not None:
            con.execute("SELECT * FROM t WHERE t MATCH ?", (query,)).fetchall()
    assert search.build_query("   ") is None
    assert search.build_query("-only") is None
    assert search.build_query('"real battle" -tunnel gram') == '("real battle" "gram"*) NOT "tunnel"*'
    assert search.build_query("giants on the earth") == '"giants"* "on" "the" "earth"*'
    assert search.build_query("на") == '"на"*'  # non-Latin words always match as prefixes


def test_markdown_text_strips_table_syntax():
    md = "# Participles\n\n| | Active |\n|---|---|\n| Present | читаемый<br>x |\n- [x] done"
    assert search.markdown_text(md) == "Participles\nActive\nPresent   читаемый x\ndone"


def test_update_and_search(env, tmp_path):
    _library(env, tmp_path)
    lib = env.library()
    result = search.update(lib)
    assert result["ok"] and result["indexed"] == 3 and result["failed"] == []

    hits = search.search("dovetail")
    assert {(h["source"], h["page"]) for h in hits} == {("highlight", 2), ("pdf", 2)}
    assert hits[0]["source"] == "highlight"  # the reader's own marks rank above book text
    assert "‹dovetail›" in hits[0]["snippet"] and "<b>dovetail</b>" in hits[0]["snippetHtml"]
    assert hits[0]["id"] == BOOK and hits[0]["path"] == "/Books/Woodworking Basics"

    lesson = search.search("lesson")
    assert lesson[0]["source"] == "name" and lesson[0]["id"] == NOTE and lesson[0]["page"] is None
    assert any(h["source"] == "pdf" and h["page"] == 3 for h in lesson)

    assert search.search("падеж")[0]["source"] == "typed"  # prefix finds падежи too
    assert [h["page"] for h in search.search("родительн")] == [1]
    assert search.search("학교")[0]["id"] == OTHER  # Korean word with a particle attached
    assert search.search('"mark the tails"')[0]["page"] == 2
    assert search.search('"tails mark"') == []
    assert search.search("dovetail -joint") == []
    assert search.search("") == []

    stats = search.stats()
    assert stats["documents"] == 3 and stats["pages"] == 6 and stats["handwritingPages"] == 0
    assert stats["size"] > 0


def test_incremental_update_rename_transcripts_and_removal(env, tmp_path):
    _library(env, tmp_path)
    search.update(env.library())

    env.opened.clear()
    again = search.update(env.library())
    assert again["indexed"] == 0 and again["unchanged"] == 3 and env.opened == []

    # Moved on the tablet without other changes: no download needed.
    entry = env.docs[OTHER][0]
    entry.path, entry.name = "/Lists/Groceries", "Groceries"
    moved = search.update(env.library())
    assert moved["renamed"] == 1 and env.opened == []
    assert search.search("groceries")[0]["path"] == "/Lists/Groceries"

    # A new transcript makes the document re-index.
    _e, doc = env.open_document(NOTE)
    transcribe._store(doc, doc.pages[1], "| Present | служащий |\n|---|---|")
    env.opened.clear()
    assert search.update(env.library())["indexed"] == 1 and env.opened == [NOTE]
    hit = search.search("служащ")[0]
    assert (hit["source"], hit["page"], hit["snippet"]) == ("handwriting", 2, "Present ‹служащий›")
    assert search.stats()["handwritingPages"] == 1

    # Gone from the library: gone from the index.
    del env.docs[BOOK]
    assert search.update(env.library())["removed"] == 1
    assert search.search("dovetail") == []


def test_full_update_rereads_everything(env, tmp_path):
    _library(env, tmp_path)
    search.update(env.library())
    env.opened.clear()
    assert search.update(env.library(), full=True)["indexed"] == 3
    assert sorted(env.opened) == sorted([BOOK, NOTE, OTHER])


def test_one_broken_document_does_not_stop_the_rest(env, tmp_path):
    _library(env, tmp_path)
    broken = tmp_path / "broken.rmdoc"
    broken.write_bytes(b"not a zip")
    env.add("d0000000-0000-4000-8000-000000000004", "/Broken", broken)
    result = search.update(env.library())
    assert result["indexed"] == 3 and len(result["failed"]) == 1
    assert result["failed"][0]["path"] == "/Broken"


def test_concurrent_update_reports_busy(env, tmp_path):
    _library(env, tmp_path)
    with search._update_lock():
        assert search.update(env.library())["busy"] is True


def test_commands(env, tmp_path):
    _library(env, tmp_path)
    search.update(env.library())
    out = search.cmd_search(argparse.Namespace(query=["dove"], limit=5))
    assert out["ok"] and out["count"] == 2
    assert set(out["results"][0]) >= {"id", "name", "path", "page", "source", "snippet"}
    assert search.cmd_search(argparse.Namespace(query=["  "], limit=5))["ok"] is False
    stats = search.cmd_stats(argparse.Namespace())
    assert stats["ok"] and {"documents", "pages", "handwritingPages", "size"} <= set(stats)


def test_search_before_any_index(env):
    out = search.cmd_search(argparse.Namespace(query=["anything"], limit=5))
    assert out["ok"] and out["results"] == [] and "hint" in out


@pytest.mark.parametrize("raw", ["NEAR(", "*", '"', "AND OR", "-"])
def test_search_never_errors_on_syntax(env, tmp_path, raw):
    _library(env, tmp_path)
    search.update(env.library())
    assert isinstance(search.search(raw), list)
