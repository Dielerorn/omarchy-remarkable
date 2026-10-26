"""Search everything on the tablet: names, typed text, highlights, handwriting
transcripts and the text of PDFs and EPUBs, in one SQLite FTS5 index.

The index is a cache: it can be deleted at any time and rebuilt with
`index update --all`.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from . import cloud, config, library, notes, transcribe
from .doc import Document

INDEX_VERSION = "2"  # bump when what gets indexed changes; forces a rebuild
SOURCES = ("name", "typed", "highlight", "handwriting", "pdf")
# bm25 is negative (lower = better), so a bigger multiplier ranks higher.
BOOST = {"name": 4.0, "highlight": 1.6, "typed": 1.4, "handwriting": 1.4, "pdf": 1.0}
HIT_START, HIT_END = "\x01", "\x02"  # placeholders, swapped for the UI's markers

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, modified TEXT,
    type TEXT, pages INTEGER, transcripts REAL DEFAULT 0, indexed REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    text, doc_id UNINDEXED, page UNINDEXED, source UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2", prefix = '2 3'
);
"""


class Busy(RuntimeError):
    pass


# ------------------------------------------------------------------ storage


def connect() -> sqlite3.Connection:
    config.INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.INDEX_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")  # searches keep working during an update
    con.executescript(SCHEMA)
    row = con.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    if row is None or row[0] != INDEX_VERSION:
        with con:
            con.execute("DELETE FROM chunks")
            con.execute("DELETE FROM docs")
            con.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (INDEX_VERSION,))
    return con


@contextmanager
def _update_lock() -> Iterator[None]:
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.RUNTIME_DIR / "index.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("the search index is already being updated")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ----------------------------------------------------------------- extract


@dataclass
class Item:
    id: str
    name: str
    path: str
    modified: str


def _clean(text: str) -> str:
    text = re.sub(r"(\w)-\n(?=[a-zа-яёåäö])", r"\1", text)  # rejoin words hyphenated at line ends
    return " ".join(text.split())


def markdown_text(md: str) -> str:
    """Transcript Markdown as plain text, so snippets don't show table pipes."""
    lines = []
    for line in md.splitlines():
        if re.fullmatch(r"\s*\|?[\s:|-]*-[\s:|-]*\|?\s*", line):
            continue  # table separator row
        line = re.sub(r"^\s*(#{1,6}\s+|>\s?|[-*+]\s+(\[[ xX]\]\s+)?|\d+[.)]\s+)", "", line)
        line = line.replace("<br>", " ").replace("|", " ").replace("**", "").replace("__", "")
        if line.strip():
            lines.append(line.strip())
    return "\n".join(lines)


def name_text(item: Item) -> str:
    folder = item.path.rsplit("/", 1)[0].strip("/")
    return item.name + (f"\n{folder.replace('/', ' / ')}" if folder else "")


def pdf_rows(doc: Document) -> Iterator[tuple[str, int]]:
    """(text, 1-based tablet page) for the document's PDF or EPUB text."""
    import pymupdf
    if doc.pdf:
        with pymupdf.open(stream=doc.pdf, filetype="pdf") as pdf:
            shown: dict[int, int] = {}  # PDF page -> first tablet page showing it
            for page in doc.pages:
                if page.pdf_page is not None and page.pdf_page < pdf.page_count:
                    shown.setdefault(page.pdf_page, page.index)
            for pdf_page, index in sorted(shown.items()):
                text = _clean(pdf[pdf_page].get_text("text"))
                if text:
                    yield text, index + 1
    elif doc.epub:
        # The tablet paginates EPUBs itself; PyMuPDF's pages map proportionally.
        with pymupdf.open(stream=doc.epub, filetype="epub") as book:
            count = book.page_count or 1
            total = len(doc.pages) or count
            for k in range(book.page_count):
                text = _clean(book[k].get_text("text"))
                if text:
                    yield text, min(total - 1, k * total // count) + 1


def document_rows(item: Item, doc: Document) -> list[tuple[str, int, str]]:
    """(text, page, source) rows for one document; page 0 = the whole document."""
    rows: list[tuple[str, int, str]] = [(name_text(item), 0, "name")]
    for p in notes.extract(doc):
        page = p.index + 1
        if p.plain:
            rows.append((p.plain, page, "typed"))
        if p.highlights:
            rows.append(("\n".join(h.text for h in p.highlights), page, "highlight"))
        if p.handwriting:
            rows.append((markdown_text(p.handwriting), page, "handwriting"))
    rows.extend((text, page, "pdf") for text, page in pdf_rows(doc))
    return rows


# ------------------------------------------------------------------ update


def _items(lib: cloud.Library | None) -> tuple[list[Item], bool]:
    """Documents to index, and whether the list came from the offline cache."""
    if lib is None:
        try:
            lib = library.fetch_library()
        except cloud.CloudError:
            entries = [e for e in library.cached_entries() if not e.get("folder") and not e.get("trashed")]
            if not entries:
                raise
            return [Item(e["id"], e["name"], e.get("path", ""), e.get("modified", "")) for e in entries], True
    return [Item(e.id, e.name, e.path, e.modified) for e in lib.documents()], False


def _replace_rows(con: sqlite3.Connection, item: Item, doc: Document, rows: list[tuple[str, int, str]],
                  transcripts: float) -> None:
    with con:
        con.execute("DELETE FROM chunks WHERE doc_id = ?", (item.id,))
        con.executemany("INSERT INTO chunks (text, doc_id, page, source) VALUES (?, ?, ?, ?)",
                        [(text, item.id, page, source) for text, page, source in rows])
        con.execute("INSERT OR REPLACE INTO docs (id, name, path, modified, type, pages, transcripts, indexed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (item.id, item.name, item.path, item.modified, doc.file_type, len(doc.pages), transcripts,
                     time.time()))


def update(lib: cloud.Library | None = None, *, full: bool = False, report=None) -> dict:
    """Bring the index up to date with the library (daemon hook).

    Only documents whose modified time, transcripts or location changed are
    re-read; documents gone from the library are dropped. full=True re-reads
    everything. report(message, done, total) gets progress.
    """
    started = time.time()
    try:
        with _update_lock():
            items, offline = _items(lib)
            con = connect()
            try:
                was_empty = con.execute("SELECT count(*) FROM docs").fetchone()[0] == 0
                result = _update(con, items, full=full, report=report)
                with con:
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('updated', ?)", (str(time.time()),))
                if full or was_empty or result["removed"]:
                    # Rewritten rows leave free pages behind; hand them back to the disk.
                    con.execute("INSERT INTO chunks (chunks) VALUES ('optimize')")
                    con.commit()
                    con.execute("VACUUM")
            finally:
                con.close()
    except Busy as e:
        return {"ok": True, "busy": True, "message": str(e)}
    result.update(ok=True, offline=offline, seconds=round(time.time() - started, 1))
    return result


def _update(con: sqlite3.Connection, items: list[Item], *, full: bool, report) -> dict:
    known = {r["id"]: r for r in con.execute("SELECT id, name, path, modified, transcripts FROM docs")}
    live = {i.id for i in items}
    removed = [doc_id for doc_id in known if doc_id not in live]
    with con:
        for doc_id in removed:
            con.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            con.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
    indexed, renamed, failed = 0, 0, []
    for n, item in enumerate(items, 1):
        row = known.get(item.id)
        transcripts = transcribe.last_change(item.id)
        same_content = row is not None and row["modified"] == item.modified and row["transcripts"] == transcripts
        if same_content and not full:
            if (row["name"], row["path"]) != (item.name, item.path):
                with con:  # moved or renamed without other changes: no need to download
                    con.execute("DELETE FROM chunks WHERE doc_id = ? AND source = 'name'", (item.id,))
                    con.execute("INSERT INTO chunks (text, doc_id, page, source) VALUES (?, ?, 0, 'name')",
                                (name_text(item), item.id))
                    con.execute("UPDATE docs SET name = ?, path = ? WHERE id = ?", (item.name, item.path, item.id))
                renamed += 1
            continue
        if report:
            report(f"indexing {item.path}", n - 1, len(items))
        try:
            _entry, doc = library.open_document(item.id)
            rows = document_rows(item, doc)
        except Exception as e:  # one unreadable document mustn't stop the rest
            failed.append({"id": item.id, "path": item.path, "error": f"{type(e).__name__}: {e}"})
            continue
        _replace_rows(con, item, doc, rows, transcripts)
        indexed += 1
    if report:
        report("done", len(items), len(items))
    return {"documents": len(items), "indexed": indexed, "renamed": renamed, "removed": len(removed),
            "unchanged": len(items) - indexed - renamed - len(failed), "failed": failed}


# ------------------------------------------------------------------ search


_PHRASE = re.compile(r'"([^"]*)"')
_WORD = re.compile(r"\w+", re.UNICODE)


def _term(words: list[str], *, prefix: bool, last_term: bool = False) -> str:
    quoted = '"' + " ".join(words) + '"'
    word = words[-1]
    # Prefix matching finds inflected forms (Russian, Swedish) and words with
    # Korean particles attached, and lets the last word be half-typed. Short
    # Latin words stay exact mid-query, or "the" would also find "there".
    if prefix and (not word.isascii() or len(word) >= 4 or (last_term and len(word) > 1)):
        quoted += "*"
    return quoted


def build_query(raw: str) -> str | None:
    """User input -> a safe FTS5 query, or None if nothing searchable is left.

    Words match as prefixes ("gram" finds "grammar"), "quoted phrases" match
    exactly, and -word excludes. Every token is re-quoted from \\w runs, so no
    input can produce an FTS syntax error.
    """
    positive: list[str] = []
    negative: list[str] = []
    for phrase in _PHRASE.findall(raw):
        words = _WORD.findall(phrase)
        if words:
            positive.append(_term(words, prefix=False))
    rest = _PHRASE.sub(" ", raw).replace('"', " ")
    chunks = [c for c in rest.split() if _WORD.search(c)]
    for n, chunk in enumerate(chunks):
        exclude = chunk.startswith("-") and len(chunk) > 1
        words = _WORD.findall(chunk)
        term = _term(words, prefix=True, last_term=n == len(chunks) - 1 and not raw.rstrip().endswith('"'))
        (negative if exclude else positive).append(term)
    if not positive:
        return None
    query = " ".join(positive)
    if negative:
        query = f"({query})" + "".join(f" NOT {t}" for t in negative)
    return query


def _markers(snippet: str) -> tuple[str, str]:
    snippet = " ".join(snippet.split())  # one line for the UI; the markers aren't whitespace
    plain = snippet.replace(HIT_START, "‹").replace(HIT_END, "›")
    marked = html.escape(snippet).replace(HIT_START, "<b>").replace(HIT_END, "</b>")
    return plain, marked


def search(raw: str, limit: int = 30) -> list[dict]:
    query = build_query(raw)
    if query is None or not config.INDEX_DB.exists():
        return []
    boost = " ".join(f"WHEN '{s}' THEN {b}" for s, b in BOOST.items())
    sql = f"""
        SELECT chunks.doc_id AS id, chunks.page AS page, chunks.source AS source,
               snippet(chunks, 0, ?, ?, '…', 12) AS snip,
               bm25(chunks) * (CASE chunks.source {boost} ELSE 1.0 END) AS score,
               d.name AS name, d.path AS path, d.type AS type
        FROM chunks JOIN docs d ON d.id = chunks.doc_id
        WHERE chunks MATCH ?
        ORDER BY score
        LIMIT ?
    """
    con = connect()
    try:
        rows = con.execute(sql, (HIT_START, HIT_END, query, max(1, min(limit, 500)))).fetchall()
    finally:
        con.close()
    results = []
    for r in rows:
        plain, marked = _markers(r["snip"] or "")
        results.append({
            "id": r["id"], "name": r["name"], "path": r["path"], "type": r["type"],
            "page": int(r["page"]) if r["page"] else None, "source": r["source"],
            "snippet": plain, "snippetHtml": marked, "score": round(-float(r["score"]), 3),
        })
    return results


def stats() -> dict:
    """documents/pages = what's indexed; handwritingPages = pages with a transcript."""
    if not config.INDEX_DB.exists():
        return {"documents": 0, "pages": 0, "handwritingPages": 0, "size": 0, "chunks": {}, "updated": None}
    con = connect()
    try:
        docs, pages = con.execute("SELECT count(*), coalesce(sum(pages), 0) FROM docs").fetchone()
        chunks = {r[0]: r[1] for r in con.execute("SELECT source, count(*) FROM chunks GROUP BY source")}
        types = {r[0]: r[1] for r in con.execute("SELECT type, count(*) FROM docs GROUP BY type")}
        row = con.execute("SELECT value FROM meta WHERE key = 'updated'").fetchone()
    finally:
        con.close()
    size = sum(p.stat().st_size for p in config.INDEX_DB.parent.glob(config.INDEX_DB.name + "*") if p.is_file())
    return {"documents": docs, "pages": pages, "handwritingPages": chunks.get("handwriting", 0), "size": size,
            "types": types, "chunks": chunks, "updated": float(row[0]) if row else None,
            "path": str(config.INDEX_DB)}


# ---------------------------------------------------------------- commands


def cmd_update(args: argparse.Namespace) -> dict:
    from .cli import progress
    return update(full=args.all, report=progress)


def cmd_stats(args: argparse.Namespace) -> dict:
    return {"ok": True, **stats()}


def cmd_search(args: argparse.Namespace) -> dict:
    raw = " ".join(args.query).strip()
    if not raw:
        return {"ok": False, "error": "type something to search for"}
    try:
        results = search(raw, args.limit)
    except sqlite3.Error as e:
        return {"ok": False, "error": f"search failed: {e}"}
    out = {"ok": True, "query": raw, "count": len(results), "results": results}
    if not config.INDEX_DB.exists():
        out["hint"] = "the search index hasn't been built yet (index update)"
    return out


def register(sub) -> None:
    p = sub.add_parser("index", help="the search index")
    isub = p.add_subparsers(dest="action", required=True)
    x = isub.add_parser("update", help="index new and changed documents")
    x.add_argument("--all", action="store_true", help="re-read every document")
    x.set_defaults(func=cmd_update)
    x = isub.add_parser("stats", help="index size and contents")
    x.set_defaults(func=cmd_stats)

    s = sub.add_parser("search", help='search names, text, highlights and handwriting ("phrase", -exclude)')
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_search)
