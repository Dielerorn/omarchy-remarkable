"""Document names, searches and links never go on a command line: every
local account can read the process list (/proc/<pid>/cmdline)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from rmbridge import cli, cloud

SECRET = "Secret Diary 'Ü'"


def test_notify_goes_over_dbus_not_a_process(monkeypatch):
    sent = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_and_get_reply(self, msg, timeout=None):
            sent.append(msg)

    import jeepney.io.blocking
    monkeypatch.setattr(jeepney.io.blocking, "open_dbus_connection", lambda bus="SESSION": Conn())

    def no_processes(*a, **k):
        raise AssertionError("notify started a process")
    monkeypatch.setattr(subprocess, "Popen", no_processes)
    monkeypatch.setattr(subprocess, "run", no_processes)
    cli.notify("New on your reMarkable", SECRET, urgency="critical")
    (msg,) = sent
    assert msg.header.fields[3] == "Notify"  # HeaderFields.member
    assert msg.body[3:5] == ("New on your reMarkable", SECRET)
    assert msg.body[6] == {"urgency": ("y", 2)}


@pytest.fixture
def fake_rmapi(tmp_path, monkeypatch):
    """A stand-in rmapi that records its argv and stdin, and answers like the shell."""
    log = tmp_path / "calls.jsonl"
    exe = tmp_path / "rmapi"
    exe.write_text(f"""#!{sys.executable}
import json, sys
data = sys.stdin.read()
with open({str(log)!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": data}}) + "\\n")
if "missing" in data:
    print("ReMarkable Cloud API Shell, User: x, SyncVersion: 1.5")
    print("Error: file doesn't exist")
    print("EOF")
elif data:
    print("ReMarkable Cloud API Shell, User: x, SyncVersion: 1.5")
    print("EOF")
else:
    print("[]")
""")
    exe.chmod(0o755)
    monkeypatch.setattr(cloud, "ensure_rmapi", lambda: exe)
    monkeypatch.setattr(cloud, "is_paired", lambda: True)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()]
    return calls


def test_rmapi_names_go_through_stdin(fake_rmapi):
    cloud.run(["mv", f"/Journals/{SECRET}", "/trash"])
    cloud.run(["mkdir", f"/{SECRET}"])
    cloud.run(["put", f"/tmp/x/{SECRET}.pdf", "/Books"])
    cloud.run(["find", "/"], json_out=True)
    calls = fake_rmapi()
    for call in calls:
        assert not any("Secret" in a for a in call["argv"]), call
    assert calls[0]["argv"] == ["-ni"] and "Secret Diary" in calls[0]["stdin"]
    assert calls[3]["argv"] == ["-ni", "-json", "find", "/"] and calls[3]["stdin"] == ""


def test_rmapi_shell_errors_raise(fake_rmapi):
    with pytest.raises(cloud.CloudError, match="file doesn't exist"):
        cloud.run(["mv", "/missing", "/trash"])


def test_shell_output():
    out, err = cloud.shell_output("ReMarkable Cloud API Shell, User: x\n[d]\tBooks\nEOF\n")
    assert out == "[d]\tBooks\n" and err == ""
    assert cloud.shell_output("ReMarkable Cloud API Shell\nError: nope\nEOF\n")[1] == "nope"


def test_stdin_args(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(["settings", "show"]) + "\n"))
    assert cli.main([cli.STDIN_ARGS]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json\n"))
    assert cli.main([cli.STDIN_ARGS]) == 2


def test_stdin_args_leave_later_lines_for_the_command(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('["ssh", "setup"]\nhunter2\n'))
    assert cli.read_stdin_args() == ["ssh", "setup"]
    assert sys.stdin.readline() == "hunter2\n"


# ----------------------------------------------- rmapi shell line framing

# Each could end the line early, continue it, start a heredoc, or act as a
# readline editing key (^U erases the line so far, ^C interrupts).
FRAMING = ["\n", "\r", "\r\n", "\t", "\x00", "\x03", "\x15", "\x1b[A", "\x85", "\u2028", "\u2029", "<<EOF",
           "\udcff"]


@pytest.mark.parametrize("bad", FRAMING)
def test_framing_characters_never_reach_rmapi(fake_rmapi, tmp_path, bad):
    hostile = f"/Docs/report{bad}mv /Important /trash{bad}nuke"
    for args in (["mv", hostile, "/trash"], ["mv", "/Docs/x", hostile], ["mkdir", hostile],
                 ["put", str(tmp_path / f"x{bad}.pdf"), "/"]):
        with pytest.raises(cloud.CloudError, match="can't be passed to rmapi safely"):
            cloud.run(args)
    assert not (tmp_path / "calls.jsonl").exists()  # rmapi never started


@pytest.mark.parametrize("name", ["it's", 'say "hi"', "a\\b", "back\\", "#tag", "$HOME", "-n", "~x", "*?[x]",
                                  "a  b", "Ünï 안녕 привет", "x; nuke", "x && nuke", "`nuke`", "'", '"', ""])
def test_shell_line_is_exactly_one_command(name):
    args = ["mv", f"/Docs/{name}", "/trash"]
    line = cloud.shell_line(args)
    assert line.endswith("\n") and line.count("\n") == 1
    import shlex
    assert shlex.split(line) == args


def test_hostile_upload_filename_is_one_put(fake_rmapi, tmp_path):
    """A file someone else named, sent as-is: still exactly one put, of a
    staged copy with a clean name, and nothing else."""
    import pymupdf
    hostile = tmp_path / "in\nmv 'x' y\nnuke\n" / "Report\nmv Important trash\r\x15nuke.pdf"
    hostile.parent.mkdir()
    d = pymupdf.open()
    d.new_page()
    d.save(hostile)
    cloud.upload(hostile, "")
    lines = [c["stdin"] for c in fake_rmapi() if c["stdin"]]
    assert len(lines) == 1 and lines[0].count("\n") == 1
    import shlex
    cmd = shlex.split(lines[0])
    assert cmd[0] == "put" and len(cmd) == 3 and cmd[2] == "/"
    assert Path(cmd[1]).name == "Report mv Important trash  nuke.pdf"
    assert not cloud.framing_unsafe(cmd[1])


def test_send_folder_subfolder_names_are_cleaned(tmp_path):
    import os
    from rmbridge import config, mirror
    send = tmp_path / "send"
    sub = send / "Books\nmv x y" / "Deep\u2028er"
    sub.mkdir(parents=True)
    f = sub / "a.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    os.utime(f, (1, 1))
    settings = dict(config.DEFAULTS, sendDir=str(send), uploadFolder="")
    assert mirror.pending_sends(settings) == [(f, "Books mv x y/Deep er")]
