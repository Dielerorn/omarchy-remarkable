"""goMarkableStream deploy/start/stop and the ssh tar.zst backup."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import CannedRemote
from rmbridge import cli, ssh, tablet_stream
from rmbridge import tablet_remote as tr


def run_cli(argv, capsys) -> dict:
    code = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return {"code": code, **json.loads(out[-1])}


@pytest.fixture
def fake_release(monkeypatch):
    payload = b"\x7fELF fake gomarkablestream"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(tablet_stream, "URL", f"http://127.0.0.1:{httpd.server_address[1]}/bin")
    monkeypatch.setattr(tablet_stream, "SHA256", hashlib.sha256(payload).hexdigest())
    yield payload
    httpd.shutdown()


def test_binary_download_is_verified(fake_release, monkeypatch, isolated):
    path = tablet_stream.ensure_binary()
    assert path.read_bytes() == fake_release and path.stat().st_mode & 0o111
    path.unlink()
    monkeypatch.setattr(tablet_stream, "SHA256", "0" * 64)
    with pytest.raises(tr.TabletError, match="checksum"):
        tablet_stream.ensure_binary()
    assert not list(path.parent.glob(".*"))  # no half-written leftovers


def test_pinned_checksum_is_the_released_one():
    # Verified 2026-10-26 against the GitHub API digest and checksums.txt for v1.4.0.
    assert tablet_stream.SHA256 == "401de5ca34116ca15db2e2710f5f433c37b7df84dd91db3891cfeea6ddf5c0e6"
    assert tablet_stream.URL.endswith("/v1.4.0/gomarkablestream-RMPRO")


def test_credentials_are_private_and_stable(isolated):
    a = tablet_stream.credentials()
    b = tablet_stream.credentials()
    assert a == b and len(a["password"]) >= 20
    assert oct((isolated / "config/stream.json").stat().st_mode & 0o777) == "0o600"


def test_usb_is_plain_http_on_the_cable_only():
    assert tablet_stream.mode_for("10.11.99.1", None) == (False, "10.11.99.1:2001")
    assert tablet_stream.mode_for("192.168.0.189", None) == (True, ":2001")
    assert tablet_stream.mode_for("10.11.99.1", True) == (True, "10.11.99.1:2001")


def test_stream_start_deploys_and_hides_password(fake_release, monkeypatch, capsys):
    state = {"running": False}

    def find(script, inp):
        if "kill" in script:
            state["running"] = False
            return "killed: 4242\n"
        return "pids: 4242\n" + '{"tls": false}\n' if state["running"] else "pids:\n"

    def launch(script, inp):
        state["running"] = True
        return "started\n"

    remote = CannedRemote({"setsid nohup": launch, "/proc/[0-9]*": find, "md5sum": "", "cat >": ""})
    monkeypatch.setattr(tr, "factory", lambda host=None, user=None: remote)
    monkeypatch.setattr(tablet_stream, "wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tablet_stream, "tcp_open", lambda *a, **k: True)
    monkeypatch.setattr(tr, "tcp_open", lambda *a, **k: True)

    result = run_cli(["ssh", "stream", "start"], capsys)
    assert result["ok"] and result["started"] and result["deployed"]
    assert result["url"] == "http://10.11.99.1:2001/" and result["username"] == "omarchy"
    upload = next(i for s, i in remote.scripts if "cat >" in s)
    assert upload == fake_release
    launch_script, launch_input = next((s, i) for s, i in remote.scripts if "setsid nohup" in s)
    assert result["password"] not in launch_script  # only on stdin
    assert launch_input == f"omarchy\n{result['password']}\n".encode()
    assert "RK_HTTPS=false" in launch_script and "RK_SERVER_BIND_ADDR=10.11.99.1:2001" in launch_script

    status = run_cli(["ssh", "stream", "status"], capsys)
    assert status["ok"] and status["running"] and status["pids"] == [4242] and status["password"]

    again = run_cli(["ssh", "stream", "start"], capsys)
    assert again["ok"] and again["started"] is False

    stopped = run_cli(["ssh", "stream", "stop"], capsys)
    assert stopped["ok"] and stopped["stopped"] == [4242]
    kill_script = next(s for s, _ in remote.scripts if "kill" in s)
    assert 'case "$exe" in /home/root/.omarchy-remarkable/gomarkablestream*)' in kill_script


def test_stream_find_script_under_busybox(sandbox):
    # The process finder must at least run cleanly in BusyBox sh with nothing to find.
    out = sandbox.run(tablet_stream._FIND + 'echo "pids:$found"').text
    assert out.strip() == "pids:"


def test_stream_start_failure_reports_log(fake_release, monkeypatch):
    remote = CannedRemote({"tail -n": "listen tcp 10.11.99.1:2001: bind: address already in use\n",
                           "/proc/[0-9]*": "pids:\n"})
    monkeypatch.setattr(tablet_stream, "wait_port", lambda *a, **k: False)
    with pytest.raises(tr.TabletError, match="address already in use"):
        tablet_stream.start(remote)


# ------------------------------------------------------------ backup


@pytest.fixture
def notebooks(sandbox):
    x = sandbox.path("/home/root/.local/share/remarkable/xochitl")
    (x / "aaaa").mkdir(parents=True)
    (x / "aaaa.metadata").write_text('{"visibleName": "Diary"}')
    (x / "aaaa/page.rm").write_bytes(b"reMarkable .lines file, version=6" + bytes(5000))
    return sandbox


def test_ssh_backup_streams_tar_zst_and_prunes(notebooks, isolated, capsys):
    folder = isolated / "backups" / "ssh"
    folder.mkdir(parents=True)
    for i in range(6):
        (folder / f"xochitl-2026010{i}-000000.tar.zst").write_bytes(b"old")
    (folder / "unrelated.txt").write_text("keep me")
    result = run_cli(["ssh", "backup"], capsys)
    assert result["ok"], result
    path = result["path"]
    assert path.startswith(str(folder)) and path.endswith(".tar.zst")
    listing = subprocess.run(f"zstd -dc '{path}' | tar -tf -", shell=True, capture_output=True, text=True).stdout
    assert "xochitl/aaaa.metadata" in listing and "xochitl/aaaa/page.rm" in listing
    kept = sorted(p.name for p in folder.glob("xochitl-*.tar.zst"))
    assert len(kept) == 5 and kept[-1] == path.rsplit("/", 1)[1]
    assert (folder / "unrelated.txt").exists() and not list(folder.glob("*.partial"))


def test_ssh_backup_failure_keeps_nothing(sandbox, isolated):
    # No xochitl folder: tar fails, nothing half-written is left behind.
    with pytest.raises(tr.TabletError):
        ssh.backup(isolated / "bk")
    assert list((isolated / "bk").iterdir()) == []
