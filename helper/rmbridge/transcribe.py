"""Handwriting to text with Claude, one rendered page at a time.

Engines (settings["transcribeEngine"]):
  claude-cli     Claude Code's own login (`claude -p`), no API key needed
  anthropic-api  the Messages API with ANTHROPIC_API_KEY
  off            never send anything

Each page's text is cached by page content, so a page is only ever sent again
after its strokes change. Pages without handwriting are never sent.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from rmscene import scene_items as si

from . import cloud, config, library, render
from .doc import Document, Page

TRANSCRIPTS = config.DATA_DIR / "transcripts"
RENDER_WIDTH = 1620  # the Paper Pro's own pixel width
AUTO_PAGE_CAP = 30  # per daemon run, across all documents
ENGINES = ("claude-cli", "anthropic-api", "off")
NO_HANDWRITING = "(no handwriting)"

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
# Models that accept the server-side refusal fallback; others get a plain request.
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
# Models that reject output_config.effort.
NO_EFFORT_MODELS = ("claude-sonnet-4-5", "claude-haiku-4-5", "claude-3")

# Strokes that aren't writing: highlighter over printed text, erasers.
NOT_WRITING = {si.Pen.HIGHLIGHTER_1, si.Pen.HIGHLIGHTER_2, si.Pen.SHADER, si.Pen.ERASER, si.Pen.ERASER_AREA}

SYSTEM_PROMPT = (
    "You transcribe pages handwritten on a reMarkable tablet. "
    "You reply with the transcription only, never with commentary."
)

PROMPT = """Transcribe the handwriting on this page faithfully as Markdown.

- Keep the writer's line breaks, headings, lists and numbering. Use Markdown tables for tables and "- [ ]" / "- [x]" for checkboxes.
- Keep every word in the language and script it was written in. {languages} Never translate, romanise, or correct spelling or grammar.
- Write [?] in place of a word you cannot read.
- Transcribe only the handwriting: skip printed text, typed text, and the page template (lines, grids, dots). Skip drawings, but transcribe any words written in them.
- No preamble, no explanations, no code fences. If the page has no handwriting, reply with exactly: {no_handwriting}"""


def prompt(languages: str = "") -> str:
    """The page instructions. `languages` is the transcribeLanguages setting, e.g. "English and Russian"."""
    languages = languages.strip().rstrip(".")
    hint = (f"The writer uses {languages}, often mixed on one page." if languages
            else "A page may mix several languages and scripts.")
    return PROMPT.format(languages=hint, no_handwriting=NO_HANDWRITING)


class TranscribeError(RuntimeError):
    """A transcription failed. fatal=True means later pages would fail the same way."""

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


# ------------------------------------------------------------------ pages


def has_writing(tree) -> bool:
    """True when a parsed page has pen strokes (not just highlights or typed text)."""
    if tree is None:
        return False
    anchors = render.anchor_positions(tree.root_text, render.paper_size(tree)[1])
    for line, _x, _y in render.walk_lines(tree.root, anchors):
        if line.points and line.tool not in NOT_WRITING:
            return True
    return False


def page_has_writing(page: Page) -> bool:
    return bool(page.rm) and has_writing(render.read_scene(page))


def _folder(doc_id: str) -> Path:
    return TRANSCRIPTS / doc_id


def cache_path(doc: Document, page: Page) -> Path:
    return _folder(doc.id) / f"{page.index}-{render.page_cache_key(doc, page)}.md"


def _files_by_key(doc_id: str) -> dict[str, Path]:
    folder = _folder(doc_id)
    if not folder.is_dir():
        return {}
    out: dict[str, Path] = {}
    for f in folder.glob("*.md"):
        _index, _, key = f.stem.partition("-")
        if key:
            out[key] = f
    return out


def cached(doc: Document) -> dict[int, str]:
    """Transcripts of the pages as they are now: {page index: markdown}.

    Looked up by page content rather than position, so moving or inserting
    pages on the tablet doesn't lose (or resend) anything. Pages Claude found
    empty map to "".
    """
    files = _files_by_key(doc.id)
    if not files:
        return {}
    out: dict[int, str] = {}
    for page in doc.pages:
        if not page.rm:
            continue
        f = files.get(render.page_cache_key(doc, page))
        if f is not None:
            try:
                out[page.index] = f.read_text().strip()
            except OSError:
                pass
    return out


def last_change(doc_id: str) -> float:
    """When this document's transcripts last changed (0 if it has none)."""
    try:
        return _folder(doc_id).stat().st_mtime
    except OSError:
        return 0.0


def _store(doc: Document, page: Page, text: str) -> Path:
    target = cache_path(doc, page)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(text + ("\n" if text else ""))
    tmp.replace(target)
    return target


def prune(doc: Document) -> None:
    """Drop transcripts of page versions that no longer exist."""
    live = {render.page_cache_key(doc, p) for p in doc.pages if p.rm}
    for key, f in _files_by_key(doc.id).items():
        if key not in live:
            f.unlink(missing_ok=True)


def pending(doc: Document, indexes: list[int] | None = None, *, force: bool = False) -> list[int]:
    """Pages with handwriting that have no transcript for their current content."""
    have = {} if force else cached(doc)
    wanted = range(len(doc.pages)) if indexes is None else indexes
    return [i for i in wanted
            if 0 <= i < len(doc.pages) and i not in have and page_has_writing(doc.pages[i])]


def clean_reply(text: str) -> str:
    """The transcription without fences or the no-handwriting sentinel."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    if text.strip(" .()").lower() == NO_HANDWRITING.strip("()"):
        return ""
    return text


# ---------------------------------------------------------------- engines


def claude_executable() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    # The desktop shell's PATH may lack the user's shell setup (mise, ~/.local/bin).
    home = Path.home()
    for candidate in (home / ".local/share/mise/installs/claude/latest/claude", home / ".local/bin/claude",
                      home / ".claude/local/claude", home / ".local/share/mise/shims/claude"):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def claude_cli_command(exe: str, model: str) -> list[str]:
    """`claude -p` locked down to a single answer with no tools.

    The page goes in as an image block over stream-json stdin, so Claude Code
    never touches the filesystem: --tools "" removes every tool, --safe-mode
    skips the user's CLAUDE.md, hooks, plugins and MCP servers, and
    --system-prompt replaces the coding-agent prompt.
    """
    return [
        exe, "-p",
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--model", model, "--effort", "medium",
        "--tools", "", "--permission-prompts", "none",
        "--no-session-persistence", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands",
        "--system-prompt", SYSTEM_PROMPT,
    ]


def _user_message(png: bytes) -> dict:
    languages = str(config.load_settings().get("transcribeLanguages") or "")
    return {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(png).decode("ascii")}},
        {"type": "text", "text": prompt(languages)},
    ]}


CLI_ERRORS = {
    "authentication_failed": "Claude Code isn't signed in. Run `claude` in a terminal, log in, then try again.",
    "billing_error": "Claude Code can't use your account right now (billing). Check claude.ai.",
    "rate_limit": "Claude's usage limit is reached for now; try again later.",
    "model_not_found": None,  # Claude Code's own message names the model
}


def parse_cli_output(stdout: str) -> str:
    """The transcription from `claude -p --output-format stream-json` output."""
    result: dict | None = None
    error_code: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant" and event.get("error"):
            error_code = str(event["error"])
        elif event.get("type") == "result":
            result = event
    if result is None:
        raise TranscribeError("Claude Code gave no answer", fatal=True)
    # A failed run still reports subtype "success"; is_error is what tells.
    if result.get("is_error") or result.get("subtype") != "success":
        message = str(result.get("result") or "").strip()
        friendly = CLI_ERRORS.get(error_code or "")
        if error_code == "authentication_failed" or "not logged in" in message.lower():
            raise TranscribeError(CLI_ERRORS["authentication_failed"], fatal=True)
        fatal = error_code in CLI_ERRORS or result.get("api_error_status") in (401, 403, 404)
        raise TranscribeError(friendly or message or "Claude Code failed", fatal=fatal)
    if result.get("stop_reason") == "refusal":
        raise TranscribeError("Claude declined to transcribe this page")
    return str(result.get("result") or "")


def transcribe_claude_cli(png: Path, model: str, *, timeout: int = 300) -> str:
    exe = claude_executable()
    if exe is None:
        raise TranscribeError("Claude Code isn't installed (the `claude` command wasn't found).", fatal=True)
    payload = json.dumps({"type": "user", "message": _user_message(png.read_bytes())}) + "\n"
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    # An empty working dir: nothing for Claude Code to pick up as project context.
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR, prefix="claude-") as cwd:
        try:
            proc = subprocess.run(claude_cli_command(exe, model), input=payload, capture_output=True,
                                  text=True, cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TranscribeError(f"Claude Code took longer than {timeout // 60} minutes on one page")
        except OSError as e:
            raise TranscribeError(f"couldn't start Claude Code: {e}", fatal=True)
    if not proc.stdout.strip() and proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or [f"exit code {proc.returncode}"])[-1]
        raise TranscribeError(f"Claude Code failed: {detail}", fatal=True)
    return parse_cli_output(proc.stdout)


def api_request_body(png: bytes, model: str, *, fallbacks: bool = True) -> dict:
    body: dict = {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [_user_message(png)],
    }
    if not model.startswith(NO_EFFORT_MODELS):
        body["output_config"] = {"effort": "medium"}
    if fallbacks and model in FALLBACK_MODELS:
        body["fallbacks"] = "default"
    return body


def api_headers(key: str, body: dict) -> dict[str, str]:
    headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": API_VERSION}
    if "fallbacks" in body:
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    return headers


def parse_api_response(data: dict) -> str:
    if data.get("stop_reason") == "refusal":
        raise TranscribeError("Claude declined to transcribe this page")
    text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    if data.get("stop_reason") == "max_tokens":
        raise TranscribeError("the transcription was cut off (page too long)")
    return text


def _api_error(status: int, raw: bytes) -> tuple[str, str]:
    try:
        err = json.loads(raw).get("error", {})
        return str(err.get("type", "")), str(err.get("message", ""))
    except (ValueError, AttributeError):
        return "", raw.decode("utf-8", "replace")[:200]


def transcribe_anthropic_api(png: Path, model: str, *, timeout: int = 300) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise TranscribeError("No Anthropic API key: set ANTHROPIC_API_KEY, or use the claude-cli engine.",
                              fatal=True)
    image = png.read_bytes()
    fallbacks = True
    attempt = 0
    while True:
        attempt += 1
        body = api_request_body(image, model, fallbacks=fallbacks)
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), headers=api_headers(key, body),
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return parse_api_response(json.loads(resp.read()))
        except urllib.error.HTTPError as e:
            kind, message = _api_error(e.code, e.read())
            if e.code == 400 and fallbacks and "fallback" in message.lower():
                fallbacks = False  # account or model without the fallback beta: plain request
                continue
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < 4:
                retry_after = e.headers.get("retry-after") if e.headers else None
                time.sleep(min(float(retry_after), 60) if retry_after and retry_after.isdigit() else 5 * attempt)
                continue
            if e.code == 401:
                raise TranscribeError("The Anthropic API key was rejected.", fatal=True)
            if e.code == 403:
                raise TranscribeError(f"The Anthropic API refused the request: {message}", fatal=True)
            if e.code == 404:
                raise TranscribeError(f"Model {model} isn't available to this API key.", fatal=True)
            if e.code == 429:
                raise TranscribeError("Anthropic API rate limit reached; try again later.", fatal=True)
            raise TranscribeError(f"Anthropic API error {e.code}: {message or kind}", fatal=e.code >= 500)
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(3 * attempt)
                continue
            raise TranscribeError(f"Couldn't reach the Anthropic API ({e.reason}).", fatal=True)
        except TimeoutError:
            raise TranscribeError("The Anthropic API took too long to answer.")


def run_engine(engine: str, png: Path, model: str) -> str:
    if engine == "claude-cli":
        return transcribe_claude_cli(png, model)
    if engine == "anthropic-api":
        return transcribe_anthropic_api(png, model)
    if engine == "off":
        raise TranscribeError("Handwriting transcription is turned off in settings.", fatal=True)
    raise TranscribeError(f"Unknown transcription engine {engine!r}.", fatal=True)


# --------------------------------------------------------------- pipeline

Engine = Callable[[str, Path, str], str]


def transcribe_pages(doc: Document, indexes: list[int], *, engine: str, model: str,
                     on_page: Callable[[int, int, int], None] | None = None,
                     run: Engine | None = None) -> tuple[dict[int, str], dict[int, str]]:
    """Send pages and cache the answers. Returns ({index: text}, {index: error}).

    Stops at the first fatal error (signed out, no key, bad model) and
    re-raises it, after keeping whatever finished before it.
    """
    run = run or run_engine  # looked up per call, so it can be swapped out
    done: dict[int, str] = {}
    errors: dict[int, str] = {}
    for n, i in enumerate(indexes, 1):
        if on_page:
            on_page(i, n, len(indexes))
        png = render.page_png(doc, i, width=RENDER_WIDTH)
        try:
            text = clean_reply(run(engine, png, model))
        except TranscribeError as e:
            if e.fatal:
                if done:
                    prune(doc)
                e.done = done  # type: ignore[attr-defined]  # what finished before it
                raise
            errors[i] = str(e)
            continue
        _store(doc, doc.pages[i], text)
        done[i] = text
    if done:
        prune(doc)
    return done, errors


def parse_pages(spec: str, count: int) -> list[int]:
    """"1,3-5" (1-based, as the tablet numbers pages) -> [0, 2, 3, 4]."""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                lo, hi = int(a or 1), int(b or count)
                out.extend(range(lo - 1, hi))
            else:
                out.append(int(part) - 1)
        except ValueError:
            raise ValueError(f'"{part}" isn\'t a page number or range like 3-5')
    bad = [i + 1 for i in out if not 0 <= i < count]
    if bad:
        raise ValueError(f"page {bad[0]} is outside this document ({count} pages)")
    return sorted(set(out))


def in_folders(path: str, folders: list[str]) -> bool:
    """Whether a cloud path sits in (or is) one of the given folder paths."""
    for folder in folders:
        f = "/" + str(folder).strip().strip("/")
        if f == "/":
            return True
        if path == f or path.startswith(f + "/"):
            return True
    return False


def auto(lib: cloud.Library, settings: dict, state: dict) -> list[str]:
    """Daemon hook: transcribe changed pages in settings["autoTranscribeFolders"].

    Sends at most AUTO_PAGE_CAP pages per run, newest documents first; a
    document is only marked done (state["transcribeChecked"][id] = modified)
    once all its pages are through, so a capped document carries on next run.
    Returns the ids of documents that got new transcripts.
    """
    engine = settings.get("transcribeEngine", "claude-cli")
    folders = settings.get("autoTranscribeFolders") or []
    if engine == "off" or not folders:
        return []
    model = settings.get("transcribeModel") or config.DEFAULTS["transcribeModel"]
    checked: dict = state.setdefault("transcribeChecked", {})
    budget = AUTO_PAGE_CAP
    changed: list[str] = []
    docs = sorted((e for e in lib.documents() if in_folders(e.path, folders)),
                  key=lambda e: e.modified, reverse=True)
    for e in docs:
        if budget <= 0:
            break
        if checked.get(e.id) == e.modified:
            continue
        try:
            _entry, doc = library.open_document(e.id)
        except cloud.CloudError:
            continue
        todo = pending(doc)
        complete = len(todo) <= budget
        todo = todo[:budget]
        try:
            done, errors = transcribe_pages(doc, todo, engine=engine, model=model)
        except TranscribeError:
            break  # signed out / no key: every later page would fail too
        budget -= len(todo)
        if done:
            changed.append(e.id)
        if complete and not errors:
            checked[e.id] = e.modified
    live = {e.id for e in lib.documents()}
    for doc_id in [k for k in checked if k not in live]:
        del checked[doc_id]
    return changed


# ---------------------------------------------------------------- commands


def cmd_transcribe(args: argparse.Namespace) -> dict:
    if args.id == "status":
        if not args.status_id:
            return {"ok": False, "error": "which document? transcribe status <id>"}
        return cmd_status(args.status_id)
    if args.status_id:
        return {"ok": False, "error": f"unexpected argument {args.status_id!r}"}
    from .cli import progress
    settings = config.load_settings()
    engine = args.engine or settings.get("transcribeEngine", "claude-cli")
    model = settings.get("transcribeModel") or config.DEFAULTS["transcribeModel"]
    if engine == "off":
        return {"ok": False, "error": "Handwriting transcription is turned off in settings.", "code": "off"}
    e, doc = library.open_document(args.id)
    try:
        indexes = parse_pages(args.pages, len(doc.pages)) if args.pages else None
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    have = cached(doc)
    todo = pending(doc, indexes, force=args.force)
    wanted = list(range(len(doc.pages))) if indexes is None else indexes
    progress(f"sending {len(todo)} page{'s' if len(todo) != 1 else ''} of {doc.name} to Claude", 0, len(todo))

    def on_page(i: int, n: int, total: int) -> None:
        progress(f"transcribing page {i + 1}", n - 1, total)

    error: str | None = None
    try:
        done, errors = transcribe_pages(doc, todo, engine=engine, model=model, on_page=on_page)
    except TranscribeError as err:
        done, errors, error = getattr(err, "done", {}), {}, str(err)
    if todo:
        progress("done", len(todo), len(todo))
    now = cached(doc)
    pages = []
    for i in wanted:
        if i in errors:
            status = "error"
        elif i in done:
            status = "transcribed"
        elif i in now:
            status = "cached"
        elif i in todo:
            status = "not sent"
        else:
            status = "no handwriting"
        entry = {"page": i + 1, "status": status}
        if i in now:
            entry["text"] = now[i]
        if i in errors:
            entry["error"] = errors[i]
        pages.append(entry)
    result = {
        "ok": error is None, "id": doc.id, "name": doc.name, "path": e.path, "engine": engine, "model": model,
        "toSend": len(todo), "sent": len(done) + len(errors), "transcribed": len(done),
        "cached": sum(1 for i in wanted if i in have and i not in done), "failed": len(errors), "pages": pages,
    }
    if error:
        result["error"] = error
        result["code"] = "transcribe"
    elif errors:
        result["warning"] = f"{len(errors)} page{'s' if len(errors) != 1 else ''} failed: {next(iter(errors.values()))}"
    return result


def cmd_status(doc_id: str) -> dict:
    e, doc = library.open_document(doc_id)
    have = cached(doc)
    pages = []
    for p in doc.pages:
        if not p.rm:
            continue
        writing = page_has_writing(p)
        if not writing and p.index not in have:
            continue
        pages.append({"page": p.index + 1, "handwriting": writing, "transcribed": p.index in have,
                      "chars": len(have.get(p.index, ""))})
    pend = [p["page"] for p in pages if p["handwriting"] and not p["transcribed"]]
    settings = config.load_settings()
    return {
        "ok": True, "id": doc.id, "name": doc.name, "path": e.path, "pageCount": len(doc.pages),
        "withHandwriting": sum(1 for p in pages if p["handwriting"]), "transcribed": len(have),
        "pending": pend, "pages": pages, "engine": settings.get("transcribeEngine"),
        "auto": in_folders(e.path, settings.get("autoTranscribeFolders") or []),
    }


def register(sub) -> None:
    p = sub.add_parser("transcribe", help="handwriting to text with Claude (transcribe <id> | transcribe status <id>)")
    p.add_argument("id", help='document id, or "status"')
    p.add_argument("status_id", nargs="?", help=argparse.SUPPRESS)
    p.add_argument("--pages", help="1-based pages, e.g. 1,3-5 (default: every page with handwriting)")
    p.add_argument("--force", action="store_true", help="send pages again even if already transcribed")
    p.add_argument("--engine", choices=ENGINES, help="override the transcribeEngine setting")
    p.set_defaults(func=cmd_transcribe)
