"""Document names, searches and links never go on a command line: every
local account can read the process list (/proc/<pid>/cmdline)."""

from __future__ import annotations

import io
import json
import subprocess
import sys

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
