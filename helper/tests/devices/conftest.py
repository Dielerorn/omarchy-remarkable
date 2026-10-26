"""Fixtures for the USB and SSH device modules.

Nothing here talks to a tablet. Paths the helper writes are redirected into
tmp_path, and two kinds of fake tablet are provided:

* SandboxRemote runs the exact shell snippets the helper would send over ssh,
  under BusyBox `sh` when available (the Paper Pro ships BusyBox 1.36.1 and
  no GNU tools; set BUSYBOX=/path/to/busybox or put it on PATH), against a
  sandbox directory standing in for the tablet's filesystem. `mount` and
  `systemctl` are stubs: remount flips the sandbox's /usr between read-only
  and writable, so a write outside the remount window fails like it would.
* CannedRemote answers scripts by substring with canned Paper Pro output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from rmbridge import config  # noqa: E402
from rmbridge import tablet_remote as tr  # noqa: E402

APPLETS = ["sh", "cat", "md5sum", "mkdir", "mv", "cp", "rm", "chmod", "tail", "wc", "grep", "printf",
           "echo", "test", "[", "awk", "cut", "sync", "tar", "ls", "du", "sed", "readlink", "sleep", "kill",
           "touch", "uname", "true", "false", "od", "head", "tr", "dirname", "basename", "ln", "find"]

PAPER_PRO_OS_RELEASE = """ID=codex
NAME="Codex Linux"
VERSION="5.7.126 (scarthgap)"
VERSION_ID=5.7.126
PRETTY_NAME="Codex Linux 5.7.126 (scarthgap)"
BUILD_MODE_RM="public"
IMG_VERSION="3.27.3.0"
"""


def busybox() -> str | None:
    env = os.environ.get("BUSYBOX")
    if env and Path(env).exists():
        return env
    return shutil.which("busybox")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every config path inside tmp_path; settings point backups there too."""
    cfg, cache, data, run = (tmp_path / n for n in ("config", "cache", "data", "run"))
    for d in (cfg, cache, data, run):
        d.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "RUNTIME_DIR", run)
    monkeypatch.setattr(config, "SSH_KEY", cfg / "id_ed25519")
    monkeypatch.setattr(config, "SETTINGS_FILE", cfg / "settings.json")
    monkeypatch.setattr(config, "STATE_FILE", data / "state.json")
    monkeypatch.setattr(config, "TMP_DIR", cache / "tmp")
    (cache / "tmp").mkdir()
    config.write_json_atomic(cfg / "settings.json", {"backupDir": str(tmp_path / "backups"),
                                                      "exportDir": str(tmp_path / "exports")})
    yield tmp_path


class SandboxRemote(tr.Remote):
    """A Remote whose 'tablet' is a directory on this computer."""

    PREFIXES = ["/usr/share/remarkable", "/home/root", "/proc/mounts", "/etc/os-release", "/etc/version",
                "/sys/devices/soc0", "/sys/class/power_supply", "/data/internal", "/proc/uptime",
                "/proc/sys/kernel/hostname", "/proc/bus/input/devices"]

    def __init__(self, root: Path, host: str = "10.11.99.1"):
        super().__init__(host, "root")
        self.root = root
        self.scripts: list[str] = []
        self.bin = root.parent / f"{root.name}-bin"
        self.bin.mkdir(exist_ok=True)
        self.log = root.parent / f"{root.name}-calls.log"
        self.log.touch()
        bb = busybox()
        self.shell = [bb, "sh"] if bb else ["/bin/sh"]
        self.using_busybox = bool(bb)
        if bb:
            for applet in APPLETS:
                link = self.bin / applet
                if not link.exists():
                    link.symlink_to(bb)
        else:
            for applet in APPLETS:
                found = shutil.which(applet)
                if found and not (self.bin / applet).exists():
                    (self.bin / applet).symlink_to(found)
        usr = root / "usr"
        (root / "proc").mkdir(parents=True, exist_ok=True)
        (root / "proc/mounts").write_text("/dev/root / ext4 ro,relatime 0 0\n"
                                          "/dev/mapper/home-encrypted-disk /home ext4 rw,relatime 0 0\n")
        self._stub("mount", f"""
echo "mount $*" >> "{self.log}"
case "$*" in
  *remount,rw*) sed -i 's| / ext4 ro,| / ext4 rw,|' "{root}/proc/mounts"; chmod -R u+w "{usr}";;
  *remount,ro*) sed -i 's| / ext4 rw,| / ext4 ro,|' "{root}/proc/mounts"; chmod -R a-w "{usr}";;
esac
""")
        self._stub("systemctl", f'echo "systemctl $*" >> "{self.log}"')
        (root / "etc").mkdir(exist_ok=True)
        (root / "etc/os-release").write_text(PAPER_PRO_OS_RELEASE)
        (root / "etc/version").write_text("20260612085811\n")
        (root / "home/root").mkdir(parents=True, exist_ok=True)

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def lock_rootfs(self) -> None:
        subprocess.run(["chmod", "-R", "a-w", str(self.root / "usr")], check=True)

    def unlock_rootfs(self) -> None:
        subprocess.run(["chmod", "-R", "u+w", str(self.root / "usr")], check=False)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def path(self, tablet_path: str) -> Path:
        return self.root / tablet_path.lstrip("/")

    def rewrite(self, script: str) -> str:
        # Only where a path starts, not inside one (/home/root/backup/usr/share/...).
        pattern = "|".join(re.escape(p) for p in sorted(self.PREFIXES, key=len, reverse=True))
        script = re.sub(rf"(?<![\w./-])({pattern})", lambda m: f"{self.root}{m.group(1)}", script)
        # Functions beat BusyBox's built-in applets (it may prefer them over PATH).
        stubs = "".join(f'{n}() {{ "{self.bin / n}" "$@"; }}\n' for n in ("mount", "systemctl"))
        return stubs + script

    def run(self, script, *, input=None, timeout=30, check=True, connect_timeout=None):
        self.scripts.append(script)
        proc = subprocess.run(self.shell + ["-c", self.rewrite(script)], input=input,
                              stdin=None if input is not None else subprocess.DEVNULL,
                              capture_output=True, timeout=timeout,
                              env={"PATH": str(self.bin), "HOME": str(self.root / "home/root")})
        # Paths in the output should read as they would on the tablet.
        out = proc.stdout.replace(str(self.root).encode(), b"") if b"\0" not in proc.stdout[:512] and \
            not proc.stdout.startswith(b"\x89PNG") else proc.stdout
        result = tr.Result(proc.returncode, out, proc.stderr.replace(str(self.root).encode(), b""))
        if check and proc.returncode != 0:
            raise tr.TabletError(result.error_text or f"exit {proc.returncode}")
        return result

    def popen(self, script, *, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw):
        self.scripts.append(script)
        return subprocess.Popen(self.shell + ["-c", self.rewrite(script)], stdin=stdin, stdout=stdout,
                                stderr=stderr, env={"PATH": str(self.bin)}, **kw)


class CannedRemote(tr.Remote):
    """Answers each script with the first canned reply whose key occurs in it."""

    def __init__(self, replies: dict[str, object] | None = None, host: str = "10.11.99.1"):
        super().__init__(host, "root")
        self.replies = dict(replies or {})
        self.scripts: list[tuple[str, bytes | None]] = []

    def run(self, script, *, input=None, timeout=30, check=True, connect_timeout=None):
        self.scripts.append((script, input))
        for key, reply in self.replies.items():
            if key in script:
                if isinstance(reply, Exception):
                    raise reply
                if callable(reply):
                    reply = reply(script, input)
                if isinstance(reply, tr.Result):
                    return reply
                out = reply.encode() if isinstance(reply, str) else bytes(reply)
                return tr.Result(0, out, b"")
        return tr.Result(0, b"", b"")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "tablet"
    root.mkdir()
    remote = SandboxRemote(root)
    monkeypatch.setattr(tr, "factory", lambda host=None, user=None: remote)
    yield remote
    remote.unlock_rootfs()  # so pytest can clean up tmp_path
