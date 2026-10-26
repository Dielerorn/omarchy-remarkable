"""ssh.py: device info parsing, reachability caching, status and setup."""

from __future__ import annotations

import json
import subprocess

import pytest

from conftest import PAPER_PRO_OS_RELEASE, CannedRemote
from rmbridge import cli, ssh
from rmbridge import tablet_remote as tr

# What INFO_SCRIPT prints on a Paper Pro (values from remarkable-spec's 3.27.3.0
# measurements and KOReader's sysfs paths; the battery numbers are made up).
PAPER_PRO_INFO = f"""@@machine
reMarkable Ferrari
@@os-release
{PAPER_PRO_OS_RELEASE}@@version
20260612085811
@@update.conf
@@hostname
imx8mm-ferrari
@@uptime
273946.12 1012345.55
@@kernel
6.12.49+git-imx8mm-ferrari-g68b95e858a0a
@@df
Filesystem           1024-blocks    Used Available Capacity Mounted on
/dev/mapper/home-encrypted-disk  48568796    112564  47929504   0% /home
@@power
[max1726x_battery]
type=Battery
status=Charging
capacity=87
capacity_level=Normal
temp=271
present=1
health=Good
cycle_count=41
[max77976-charger]
type=USB
status=Charging
online=1
@@net
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
4: usb1    inet 10.11.99.1/27 brd 10.11.99.31 scope global usb1\\       valid_lft forever preferred_lft forever
8: wlan0    inet 192.168.0.189/24 brd 192.168.0.255 scope global dynamic wlan0\\       valid_lft 85000sec
@@wifissh
off
@@end
"""


def run_cli(argv, capsys) -> dict:
    code = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return {"code": code, **json.loads(out[-1])}


def test_parse_paper_pro_info():
    info = ssh.parse_info(PAPER_PRO_INFO)
    assert info["model"] == "reMarkable Paper Pro" and info["machine"] == "reMarkable Ferrari"
    assert info["firmware"] == "3.27.3.0" and info["build"] == "20260612085811"
    assert info["battery"] == 87 and info["charging"] is True and info["pluggedIn"] is True
    assert info["temperature"] == 27.1 and info["batterySource"] == "max1726x_battery"
    assert info["storageTotal"] == 48568796 * 1024 and info["storageFree"] == 47929504 * 1024
    assert info["hostname"] == "imx8mm-ferrari" and info["uptime"] == 273946
    assert info["wifiIp"] == "192.168.0.189" and info["usbIp"] == "10.11.99.1"
    assert info["wifiSsh"] is False
    assert info["os"] == "Codex Linux 5.7.126 (scarthgap)"


def test_parse_rm2_style_power_and_old_firmware():
    text = """@@machine
reMarkable 2.0
@@os-release
NAME="Codex Linux"
@@version
20230912095217
@@update.conf
[General]
REMARKABLE_RELEASE_VERSION=3.6.1.1894
@@power
[max77818-charger]
type=USB
online=0
status=Not charging
[max77818_battery]
type=Battery
status=Discharging
capacity=54
temp=301
@@end
"""
    info = ssh.parse_info(text)
    assert info["model"] == "reMarkable 2" and info["firmware"] == "3.6.1.1894"
    assert info["battery"] == 54 and info["charging"] is False and info["pluggedIn"] is False
    assert info["temperature"] == 30.1 and info["storageFree"] is None and info["wifiIp"] is None


def test_info_uses_one_ssh_call(monkeypatch):
    remote = CannedRemote({"@@machine": PAPER_PRO_INFO})
    monkeypatch.setattr(tr, "factory", lambda host=None, user=None: remote)
    info = ssh.info()
    assert info["battery"] == 87 and info["host"] == "10.11.99.1"
    assert len(remote.scripts) == 1


@pytest.fixture
def key(isolated):
    tr.ensure_key()
    return tr.key_path()


def test_ensure_key_creates_private_ed25519(isolated):
    key = tr.ensure_key()
    assert key.exists() and oct(key.stat().st_mode & 0o777) == "0o600"
    assert tr.public_key().startswith("ssh-ed25519 ")
    assert tr.ensure_key() == key  # idempotent


def test_reachable_caches_key_check(monkeypatch, key):
    calls = []
    remote = CannedRemote({"true": lambda s, i: calls.append(s) or tr.Result(0, b"", b"")})
    monkeypatch.setattr(tr, "factory", lambda host=None, user=None: remote)
    monkeypatch.setattr(tr, "tcp_open", lambda host, port=22, timeout=1.0: True)
    assert ssh.reachable() is True
    assert ssh.reachable() is True
    assert len(calls) == 1  # second answer came from the cache


def test_reachable_false_when_port_closed_clears_cache(monkeypatch, key):
    remote = CannedRemote({"true": ""})
    monkeypatch.setattr(tr, "factory", lambda host=None, user=None: remote)
    monkeypatch.setattr(tr, "tcp_open", lambda host, port=22, timeout=1.0: True)
    assert ssh.reachable()
    monkeypatch.setattr(tr, "tcp_open", lambda host, port=22, timeout=1.0: False)
    assert ssh.reachable() is False
    assert not ssh._auth_cache().exists()


def test_reachable_remembers_refusal_but_not_timeouts(monkeypatch, key):
    remote = CannedRemote({"true": tr.AuthFailed("nope")})
    monkeypatch.setattr(tr, "factory", lambda host=None, user=None: remote)
    monkeypatch.setattr(tr, "tcp_open", lambda host, port=22, timeout=1.0: True)
    assert ssh.reachable() is False
    assert json.loads(ssh._auth_cache().read_text())["ok"] is False
    ssh._clear_auth_cache()
    remote.replies["true"] = tr.Unreachable("slow")
    assert ssh.reachable() is False
    assert not ssh._auth_cache().exists()


def test_reachable_without_key_is_false_without_network(monkeypatch, isolated):
    monkeypatch.setattr(tr, "tcp_open", lambda *a, **k: pytest.fail("shouldn't probe without a key"))
    assert ssh.reachable() is False


def test_classify_ssh_errors():
    assert isinstance(tr.classify("ssh: connect to host 10.11.99.1 port 22: Connection timed out", "h"),
                      tr.Unreachable)
    assert isinstance(tr.classify("root@10.11.99.1: Permission denied (publickey,password).", "h"), tr.AuthFailed)
    assert isinstance(tr.classify("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", "h"),
                      tr.HostKeyChanged)
    assert "rm-ssh-over-wlan" in str(tr.classify("No route to host", "h"))


def test_remote_command_line_never_uses_user_ssh_config(isolated):
    args = tr.Remote("10.11.99.1").command("true")
    joined = " ".join(args)
    assert args[:3] == ["ssh", "-F", "/dev/null"]
    assert f"UserKnownHostsFile={isolated / 'config' / 'known_hosts'}" in joined
    assert "StrictHostKeyChecking=accept-new" in joined and "BatchMode=yes" in joined
    assert f"-i {isolated / 'config' / 'id_ed25519'}" in joined and "IdentitiesOnly=yes" in joined


def test_install_key_script_is_idempotent(sandbox, key):
    pub = tr.public_key()
    auth = sandbox.path("/home/root/.ssh/authorized_keys")
    auth.parent.mkdir(parents=True)
    auth.write_text("ssh-rsa AAAAexisting user@laptop")  # no trailing newline
    script = ssh.INSTALL_KEY.format(key=tr.q(pub))
    assert "key-installed" in sandbox.run(script).text
    assert "key-installed" in sandbox.run(script).text
    lines = auth.read_text().splitlines()
    assert lines == ["ssh-rsa AAAAexisting user@laptop", pub]
    assert oct(auth.stat().st_mode & 0o777) == "0o600"
    assert oct(auth.parent.stat().st_mode & 0o777) == "0o700"


def test_setup_needs_a_password(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("\n"))
    result = run_cli(["ssh", "setup"], capsys)
    assert result["ok"] is False and "Copyrights and licenses" in result["error"]


def test_setup_unreachable(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("hunter2\n"))
    monkeypatch.setattr(tr, "tcp_open", lambda *a, **k: False)
    result = run_cli(["ssh", "setup"], capsys)
    assert result["ok"] is False and result["code"] == "unreachable"


def test_setup_passes_password_through_askpass(monkeypatch, capsys, isolated):
    """The password reaches ssh only through SSH_ASKPASS and is never on a command line."""
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("s3cret pass\n"))
    monkeypatch.setattr(tr, "tcp_open", lambda *a, **k: True)
    seen = {}
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "ssh":
            env = kw["env"]
            seen["argv"] = cmd
            seen["askpass"] = real_run([env["SSH_ASKPASS"]], env=env, capture_output=True, text=True).stdout
            seen["require"] = env["SSH_ASKPASS_REQUIRE"]
            return subprocess.CompletedProcess(cmd, 0, b"key-installed\n", b"")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(tr.Remote, "run", lambda self, script, **kw: tr.Result(0, b"", b""))
    result = run_cli(["ssh", "setup"], capsys)
    assert result["ok"] is True and result["keyInstalled"] is True
    assert seen["askpass"] == "s3cret pass\n" and seen["require"] == "force"
    assert not any("s3cret" in a for a in seen["argv"])
    assert "PubkeyAuthentication=no" in seen["argv"]
    assert not (isolated / "run" / "askpass.sh").exists()


def test_status_and_stream_status_when_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(tr, "tcp_open", lambda *a, **k: False)
    status = run_cli(["ssh", "status"], capsys)
    assert status["ok"] and status["reachable"] is False and "rm-ssh-over-wlan" in status["hint"]
    stream = run_cli(["ssh", "stream", "status"], capsys)
    assert stream["ok"] and stream["running"] is False
    pen = run_cli(["ssh", "pen", "status"], capsys)
    assert pen["ok"] and pen["running"] is False
