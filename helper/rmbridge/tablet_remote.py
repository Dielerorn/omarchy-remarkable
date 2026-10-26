"""SSH plumbing for developer-mode features.

Everything goes through the system `ssh` with the plugin's own key and
known_hosts (never ~/.ssh), and `-F /dev/null` so a personal ssh config with
a "remarkable" host entry can't change how we connect.

The tablet runs dropbear (socket-activated, one process per connection) and a
BusyBox userland: no GNU long options, no python, no sftp guarantee. Files are
therefore moved with `cat` over the ssh channel instead of scp, which also
sidesteps OpenSSH 9+'s scp defaulting to the SFTP protocol.

Tests replace `factory` with something that returns a fake Remote.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable

from . import config

REMOTE_HOME = "/home/root"
REMOTE_WORK = f"{REMOTE_HOME}/.omarchy-remarkable"
REMOTE_BACKUP = f"{REMOTE_HOME}/.omarchy-remarkable-backup"
USB_HOST = "10.11.99.1"
PASSWORD_WHERE = "Settings → General → Help → About → Copyrights and licenses"
WIFI_HINT = ("For Wi-Fi: connect once over USB, run `rm-ssh-over-wlan on` in an ssh session on the "
             "tablet, then set sshHost to the tablet's Wi-Fi address.")


class TabletError(RuntimeError):
    code = "tablet"


class Unreachable(TabletError):
    code = "unreachable"


class AuthFailed(TabletError):
    code = "auth"


class HostKeyChanged(TabletError):
    code = "host-key-changed"


@dataclass
class Result:
    code: int
    out: bytes
    err: bytes

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", "replace")

    @property
    def error_text(self) -> str:
        return self.err.decode("utf-8", "replace").strip()

    @property
    def ok(self) -> bool:
        return self.code == 0


def known_hosts() -> Path:
    return config.CONFIG_DIR / "known_hosts"


def key_path() -> Path:
    return config.SSH_KEY


def ensure_key() -> Path:
    """Create the plugin's own ed25519 key on first use."""
    key = key_path()
    if key.exists() and key.with_suffix(".pub").exists():
        return key
    key.parent.mkdir(parents=True, exist_ok=True)
    for stale in (key, key.with_suffix(".pub")):
        stale.unlink(missing_ok=True)
    comment = f"omarchy-remarkable@{socket.gethostname()}"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    os.chmod(key, 0o600)
    return key


def public_key() -> str:
    ensure_key()
    return key_path().with_suffix(".pub").read_text().strip()


def usb_link_up(host: str = USB_HOST) -> bool:
    """Is there a USB network link to the tablet at all?

    Plugged in, the tablet hands this computer an address in 10.11.99.0/27.
    Without that, packets for 10.11.99.1 go to the default gateway and simply
    vanish, so a connect would sit out its whole timeout. A UDP "connect"
    only asks the kernel for a route; nothing is sent.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            local = s.getsockname()[0]
    except OSError:
        return False
    parts = local.split(".")
    return parts[:3] == ["10", "11", "99"] and int(parts[3]) < 32


def tcp_open(host: str, port: int = 22, timeout: float = 1.0) -> bool:
    if host == USB_HOST and not usb_link_up(host):
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def classify(stderr: str, host: str) -> TabletError:
    """Turn ssh's own error text into something the bar can show."""
    s = stderr.lower()
    if "remote host identification has changed" in s or "host key verification failed" in s:
        return HostKeyChanged(
            f"The tablet at {host} has a different identity than last time (factory reset or a "
            "different device). Run setup again with --reset-host-key if that's expected.")
    if "permission denied" in s or "too many authentication failures" in s:
        return AuthFailed(f"The tablet at {host} didn't accept our key. Run ssh setup with the root "
                          f"password from {PASSWORD_WHERE}.")
    unreachable = ("timed out", "no route to host", "connection refused", "network is unreachable",
                   "could not resolve", "connection closed", "connection reset", "broken pipe",
                   "host is down", "name or service not known")
    if any(u in s for u in unreachable):
        return Unreachable(f"The tablet isn't answering at {host}. Plug it in by USB-C and wake it "
                           f"(developer mode must be on). {WIFI_HINT}")
    last = [l for l in stderr.strip().splitlines() if l.strip()]
    return TabletError(last[-1] if last else f"ssh to {host} failed")


class Remote:
    """One tablet over ssh. Each call is its own ssh connection."""

    def __init__(self, host: str, user: str = "root", *, port: int = 22, connect_timeout: float = 5):
        self.host = host
        self.user = user
        self.port = port
        self.connect_timeout = connect_timeout

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def ssh_args(self, *, batch: bool = True, connect_timeout: float | None = None) -> list[str]:
        args = [
            "ssh", "-F", "/dev/null", "-T",
            "-p", str(self.port),
            "-o", f"ConnectTimeout={max(1, int(round(connect_timeout or self.connect_timeout)))}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts()}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
        ]
        if batch:
            args += ["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "PasswordAuthentication=no",
                     "-o", "KbdInteractiveAuthentication=no", "-i", str(key_path())]
        return args

    def command(self, script: str, **kw) -> list[str]:
        return self.ssh_args(**kw) + [self.target, script]

    def run(self, script: str, *, input: bytes | None = None, timeout: float = 30,
            check: bool = True, connect_timeout: float | None = None) -> Result:
        """Run a shell snippet on the tablet; raise TabletError on ssh failure."""
        known_hosts().parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(self.command(script, connect_timeout=connect_timeout), input=input,
                                  stdin=None if input is not None else subprocess.DEVNULL,
                                  capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise Unreachable(f"The tablet at {self.host} stopped answering (timed out after "
                              f"{int(timeout)} s).") from e
        result = Result(proc.returncode, proc.stdout, proc.stderr)
        if proc.returncode == 255:
            raise classify(result.error_text, self.host)
        if check and proc.returncode != 0:
            raise TabletError(result.error_text or f"command failed on the tablet (exit {proc.returncode})")
        return result

    def popen(self, script: str, *, stdin: int | IO | None = subprocess.DEVNULL,
              stdout: int | IO | None = subprocess.PIPE, stderr: int | IO | None = subprocess.PIPE,
              **kw) -> subprocess.Popen:
        known_hosts().parent.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen(self.command(script), stdin=stdin, stdout=stdout, stderr=stderr, **kw)

    # -------------------------------------------------------------- files

    def get(self, path: str, *, timeout: float = 120) -> bytes:
        return self.run(f"cat {q(path)}", timeout=timeout).out

    def exists(self, path: str) -> bool:
        return self.run(f"test -e {q(path)}", check=False).code == 0

    def put(self, data: bytes, path: str, *, mode: int | None = None, timeout: float = 300) -> None:
        """Write bytes to a /home path atomically and check they arrived intact."""
        tmp = f"{path}.part"
        digest = hashlib.md5(data).hexdigest()
        chmod = f" && chmod {mode:o} {q(tmp)}" if mode is not None else ""
        script = (f"mkdir -p {q(os.path.dirname(path))} && cat > {q(tmp)}{chmod} && "
                  f"set -- $(md5sum {q(tmp)}) && [ \"$1\" = {digest} ] && mv -f {q(tmp)} {q(path)} "
                  f"|| {{ rm -f {q(tmp)}; echo 'upload damaged' >&2; exit 3; }}")
        self.run(script, input=data, timeout=timeout)


def q(value: str) -> str:
    return shlex.quote(value)


def settings_target() -> tuple[str, str]:
    settings = config.load_settings()
    return str(settings.get("sshHost") or USB_HOST), str(settings.get("sshUser") or "root")


def _default_factory(host: str | None = None, user: str | None = None) -> Remote:
    h, u = settings_target()
    return Remote(host or h, user or u)


# Tests swap this for a function returning a fake Remote.
factory: Callable[..., Remote] = _default_factory


def connect(host: str | None = None, user: str | None = None) -> Remote:
    return factory(host=host, user=user)


def wait_port(host: str, port: int, timeout: float, interval: float = 0.4) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tcp_open(host, port, timeout=min(1.0, max(0.1, deadline - time.monotonic()))):
            return True
        time.sleep(interval)
    return False
