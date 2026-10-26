"""transcribe.py, with every engine faked: nothing here talks to Claude."""

from __future__ import annotations

import argparse
import io
import json
import urllib.error

import pytest
from rmscene import scene_items as si

from rmbridge import transcribe
from rmfixtures import glyph, make_rmdoc, page_rm, stroke

DOC = "12345678-0000-4000-8000-000000000001"


def _doc(env, tmp_path, pages=None, doc_id=DOC, path="/Notes/Diary", modified="2026-01-01T00:00:00Z"):
    pages = pages if pages is not None else [page_rm(lines=[stroke()]), None,
                                            page_rm(glyphs=[glyph("only a highlight", 0)]),
                                            page_rm(lines=[stroke(y=900)])]
    rmdoc = make_rmdoc(tmp_path / f"{doc_id}-{modified}.rmdoc", doc_id, path.rsplit("/", 1)[-1], pages)
    env.add(doc_id, path, rmdoc, modified=modified)
    return env.open_document(doc_id)[1]


class FakeEngine:
    def __init__(self, replies=None, fail_on=None):
        self.calls: list[tuple[str, str, str]] = []
        self.replies = replies or {}
        self.fail_on = fail_on or {}

    def __call__(self, engine, png, model):
        n = len(self.calls)
        self.calls.append((engine, png.name, model))
        if n in self.fail_on:
            raise self.fail_on[n]
        return self.replies.get(n, f"text {n}")


# ------------------------------------------------------------------ parsing


def test_parse_pages():
    assert transcribe.parse_pages("1,3-5", 6) == [0, 2, 3, 4]
    assert transcribe.parse_pages("2-", 4) == [1, 2, 3]
    assert transcribe.parse_pages(" 3 , 3 ", 4) == [2]
    with pytest.raises(ValueError, match="outside"):
        transcribe.parse_pages("9", 4)
    with pytest.raises(ValueError, match="page number"):
        transcribe.parse_pages("x", 4)


def test_clean_reply():
    assert transcribe.clean_reply("```markdown\n# Hi\n```") == "# Hi"
    assert transcribe.clean_reply("(no handwriting)") == ""
    assert transcribe.clean_reply("  No handwriting. ") == ""
    assert transcribe.clean_reply("Привет\n\n- раз") == "Привет\n\n- раз"


# Output captured from `claude -p --output-format stream-json` (trimmed).
CLI_OK = "\n".join(json.dumps(x) for x in [
    {"type": "system", "subtype": "init", "tools": []},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "# Русский"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn",
     "result": "# Русский\n# GRAMMAR", "num_turns": 1},
])
CLI_LOGGED_OUT = "\n".join(json.dumps(x) for x in [
    {"type": "system", "subtype": "init", "apiKeySource": "none"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Not logged in · Please run /login"}]},
     "error": "authentication_failed"},
    {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login",
     "api_error_status": None, "terminal_reason": "api_error"},
])
CLI_BAD_MODEL = "\n".join(json.dumps(x) for x in [
    {"type": "assistant", "message": {"content": []}, "error": "model_not_found"},
    {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 404,
     "result": "There's an issue with the selected model (claude-x). It may not exist or you may not have access to it."},
])


def test_parse_cli_output():
    assert transcribe.parse_cli_output(CLI_OK) == "# Русский\n# GRAMMAR"
    with pytest.raises(transcribe.TranscribeError, match="isn't signed in") as e:
        transcribe.parse_cli_output(CLI_LOGGED_OUT)
    assert e.value.fatal
    with pytest.raises(transcribe.TranscribeError, match="selected model") as e:
        transcribe.parse_cli_output(CLI_BAD_MODEL)
    assert e.value.fatal
    refused = json.dumps({"type": "result", "subtype": "success", "is_error": False, "stop_reason": "refusal",
                          "result": ""})
    with pytest.raises(transcribe.TranscribeError, match="declined") as e:
        transcribe.parse_cli_output(refused)
    assert not e.value.fatal
    with pytest.raises(transcribe.TranscribeError, match="no answer"):
        transcribe.parse_cli_output("garbage\n")


def test_cli_command_has_no_tools_and_no_user_config():
    cmd = transcribe.claude_cli_command("/bin/claude", "claude-sonnet-5-5")
    assert cmd[:2] == ["/bin/claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in cmd
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"


def test_cli_engine_end_to_end_with_fake_binary(env, tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\ncat > \"$(dirname \"$0\")/stdin.json\"\n"
                    f"cat <<'EOF'\n{CLI_OK}\nEOF\n")
    fake.chmod(0o755)
    monkeypatch.setattr(transcribe, "claude_executable", lambda: str(fake))
    png = tmp_path / "page.png"
    png.write_bytes(b"\x89PNG fake")
    assert transcribe.transcribe_claude_cli(png, "claude-sonnet-5-5") == "# Русский\n# GRAMMAR"
    sent = json.loads((tmp_path / "stdin.json").read_text())
    content = sent["message"]["content"]
    assert sent["type"] == "user" and content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert "never translate" in content[1]["text"].lower()


def test_cli_engine_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "claude_executable", lambda: None)
    with pytest.raises(transcribe.TranscribeError, match="isn't installed") as e:
        transcribe.transcribe_claude_cli(tmp_path / "x.png", "m")
    assert e.value.fatal


# ---------------------------------------------------------------- API engine


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_api_engine_request_and_reply(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        return FakeResponse(json.dumps({"content": [{"type": "thinking", "thinking": ""},
                                                    {"type": "text", "text": "Hej då"}],
                                        "stop_reason": "end_turn"}).encode())

    monkeypatch.setattr(transcribe.urllib.request, "urlopen", urlopen)
    png = tmp_path / "p.png"
    png.write_bytes(b"\x89PNG")
    assert transcribe.transcribe_anthropic_api(png, "claude-sonnet-5-5") == "Hej då"
    req = seen[0]
    assert req.full_url == "https://api.anthropic.com/v1/messages"
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["x-api-key"] == "sk-test" and headers["anthropic-version"] == "2026-10-21"
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    body = json.loads(req.data)
    assert body["model"] == "claude-sonnet-5-5" and body["fallbacks"] == "default"
    assert body["messages"][0]["content"][0]["source"] == {"type": "base64", "media_type": "image/png",
                                                           "data": "iVBORw=="}
    assert "temperature" not in body


def test_api_engine_retries_without_fallbacks_and_maps_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    bodies = []

    def urlopen(req, timeout):
        body = json.loads(req.data)
        bodies.append(body)
        if "fallbacks" in body:
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(json.dumps(
                {"type": "error", "error": {"type": "invalid_request_error",
                                            "message": "fallbacks: not enabled"}}).encode()))
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error":{"type":"auth"}}'))

    monkeypatch.setattr(transcribe.urllib.request, "urlopen", urlopen)
    png = tmp_path / "p.png"
    png.write_bytes(b"\x89PNG")
    with pytest.raises(transcribe.TranscribeError, match="key was rejected") as e:
        transcribe.transcribe_anthropic_api(png, "claude-sonnet-5-5")
    assert e.value.fatal and len(bodies) == 2 and "fallbacks" not in bodies[1]


def test_api_engine_refusal_and_missing_key(monkeypatch, tmp_path):
    with pytest.raises(transcribe.TranscribeError, match="declined"):
        transcribe.parse_api_response({"stop_reason": "refusal", "content": []})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(transcribe.TranscribeError, match="ANTHROPIC_API_KEY") as e:
        transcribe.transcribe_anthropic_api(tmp_path / "p.png", "m")
    assert e.value.fatal
    assert "output_config" not in transcribe.api_request_body(b"x", "claude-haiku-4-5")
    assert "fallbacks" not in transcribe.api_request_body(b"x", "claude-haiku-5-5")


# ----------------------------------------------------------------- pipeline


def test_only_pages_with_writing_are_sent_and_cached(env, tmp_path):
    doc = _doc(env, tmp_path)
    assert transcribe.pending(doc) == [0, 3]
    engine = FakeEngine(replies={0: "Привет", 1: "(no handwriting)"})
    done, errors = transcribe.transcribe_pages(doc, transcribe.pending(doc), engine="claude-cli",
                                               model="m", run=engine)
    assert done == {0: "Привет", 3: ""} and errors == {}
    assert len(engine.calls) == 2 and engine.calls[0][1].endswith("-1620.png")
    assert transcribe.cached(doc) == {0: "Привет", 3: ""}
    assert transcribe.pending(doc) == []
    assert transcribe.pending(doc, force=True) == [0, 3]


def test_cache_follows_page_content_not_position(env, tmp_path):
    a, b = page_rm(lines=[stroke(y=100)]), page_rm(lines=[stroke(y=700)])
    doc = _doc(env, tmp_path, pages=[a, b])
    transcribe.transcribe_pages(doc, [0, 1], engine="x", model="m", run=FakeEngine({0: "A", 1: "B"}))
    # Page inserted in front and page b edited on the tablet.
    doc2 = _doc(env, tmp_path, pages=[page_rm(lines=[stroke(y=50)]), a, page_rm(lines=[stroke(y=1200)])],
                modified="2026-02-01T00:00:00Z")
    assert transcribe.cached(doc2) == {1: "A"}
    assert transcribe.pending(doc2) == [0, 2]
    transcribe.transcribe_pages(doc2, [0], engine="x", model="m", run=FakeEngine({0: "new"}))
    files = sorted(p.name.split("-", 1)[0] for p in (transcribe.TRANSCRIPTS / DOC).glob("*.md"))
    assert len(files) == 2  # the old page b transcript was pruned


def test_fatal_error_stops_but_keeps_finished_pages(env, tmp_path):
    doc = _doc(env, tmp_path)
    engine = FakeEngine(fail_on={1: transcribe.TranscribeError("signed out", fatal=True)})
    with pytest.raises(transcribe.TranscribeError, match="signed out"):
        transcribe.transcribe_pages(doc, [0, 3], engine="x", model="m", run=engine)
    assert transcribe.cached(doc) == {0: "text 0"}


def test_page_error_is_recorded_and_others_continue(env, tmp_path):
    doc = _doc(env, tmp_path)
    engine = FakeEngine(fail_on={0: transcribe.TranscribeError("declined")})
    done, errors = transcribe.transcribe_pages(doc, [0, 3], engine="x", model="m", run=engine)
    assert done == {3: "text 1"} and errors == {0: "declined"}


def test_command_reports_counts(env, tmp_path, monkeypatch):
    _doc(env, tmp_path)
    engine = FakeEngine()
    monkeypatch.setattr(transcribe, "run_engine", engine)
    args = argparse.Namespace(id=DOC, status_id=None, pages="1-3", force=False, engine=None)
    out = transcribe.cmd_transcribe(args)
    assert out["ok"] and out["toSend"] == 1 and out["transcribed"] == 1
    assert [(p["page"], p["status"]) for p in out["pages"]] == [(1, "transcribed"), (2, "no handwriting"),
                                                               (3, "no handwriting")]
    again = transcribe.cmd_transcribe(args)
    assert again["transcribed"] == 0 and again["cached"] == 1 and len(engine.calls) == 1
    status = transcribe.cmd_transcribe(argparse.Namespace(id="status", status_id=DOC, pages=None, force=False,
                                                          engine=None))
    assert status["ok"] and status["pending"] == [4] and status["transcribed"] == 1
    env.settings["transcribeEngine"] = "off"
    assert transcribe.cmd_transcribe(args)["ok"] is False


def test_command_stops_on_signed_out(env, tmp_path, monkeypatch):
    _doc(env, tmp_path)
    monkeypatch.setattr(transcribe, "run_engine",
                        FakeEngine(fail_on={0: transcribe.TranscribeError("Claude Code isn't signed in", fatal=True)}))
    out = transcribe.cmd_transcribe(argparse.Namespace(id=DOC, status_id=None, pages=None, force=False, engine=None))
    assert out["ok"] is False and "signed in" in out["error"] and out["transcribed"] == 0


# --------------------------------------------------------------------- auto


def test_auto_respects_folders_cap_and_state(env, tmp_path, monkeypatch):
    many = [page_rm(lines=[stroke(y=100 + i * 10)]) for i in range(25)]
    _doc(env, tmp_path, pages=many, doc_id="aaaaaaaa-0000-4000-8000-00000000000a", path="/Study/Big",
         modified="2026-03-01T00:00:00Z")
    _doc(env, tmp_path, pages=many[:10], doc_id="bbbbbbbb-0000-4000-8000-00000000000b", path="/Study/Small",
         modified="2026-02-01T00:00:00Z")
    _doc(env, tmp_path, doc_id="cccccccc-0000-4000-8000-00000000000c", path="/Journals/Private")
    engine = FakeEngine()
    monkeypatch.setattr(transcribe, "run_engine", engine)
    settings = dict(env.settings, autoTranscribeFolders=["/Study"])
    state: dict = {}

    assert transcribe.auto(env.library(), dict(settings, transcribeEngine="off"), state) == []
    assert transcribe.auto(env.library(), dict(settings, autoTranscribeFolders=[]), state) == []

    changed = transcribe.auto(env.library(), settings, state)
    assert len(engine.calls) == transcribe.AUTO_PAGE_CAP == 30
    assert changed == ["aaaaaaaa-0000-4000-8000-00000000000a", "bbbbbbbb-0000-4000-8000-00000000000b"]
    assert state["transcribeChecked"] == {"aaaaaaaa-0000-4000-8000-00000000000a": "2026-03-01T00:00:00Z"}

    changed = transcribe.auto(env.library(), settings, state)  # the capped document carries on
    assert len(engine.calls) == 35 and changed == ["bbbbbbbb-0000-4000-8000-00000000000b"]
    assert len(state["transcribeChecked"]) == 2
    assert not any("cccccccc" in c[1] for c in engine.calls)

    env.opened.clear()
    assert transcribe.auto(env.library(), settings, state) == [] and env.opened == []


def test_auto_stops_everything_on_fatal_error(env, tmp_path, monkeypatch):
    _doc(env, tmp_path, path="/Study/One")
    monkeypatch.setattr(transcribe, "run_engine",
                        FakeEngine(fail_on={0: transcribe.TranscribeError("no key", fatal=True)}))
    state: dict = {}
    assert transcribe.auto(env.library(), dict(env.settings, autoTranscribeFolders=["/Study"]), state) == []
    assert state["transcribeChecked"] == {}


def test_in_folders():
    assert transcribe.in_folders("/Study/Russian/Notes", ["/Study"])
    assert transcribe.in_folders("/Study/Russian/Notes", ["Study/Russian/"])
    assert not transcribe.in_folders("/Studying/x", ["/Study"])
    assert not transcribe.in_folders("/x", [])


def test_highlighter_only_page_is_not_writing():
    from rmbridge import render
    from rmbridge.doc import Page
    rm = page_rm(lines=[stroke(tool=si.Pen.HIGHLIGHTER_1, color=si.PenColor.YELLOW)])
    assert not transcribe.has_writing(render.read_scene(Page(index=0, id="p", rm=rm)))


def test_prompt_names_the_writers_languages_only_when_set():
    assert "may mix several languages" in transcribe.prompt("")
    named = transcribe.prompt(" English and Russian. ")
    assert "The writer uses English and Russian, often mixed on one page." in named
    assert named.endswith(transcribe.NO_HANDWRITING)
