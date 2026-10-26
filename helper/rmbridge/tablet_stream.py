"""Live screen through goMarkableStream (github.com/owulveryck/goMarkableStream).

The binary is downloaded once, checked against a pinned sha256, copied to
/home/root/.omarchy-remarkable/ on the tablet and started detached with
setsid/nohup (no systemd unit, nothing survives a reboot). It serves a web
page with a login form on port 2001; credentials come from RK_SERVER_USERNAME
and RK_SERVER_PASSWORD, which we set to a random pair kept 0600 in CONFIG_DIR.

Over the USB cable we bind to 10.11.99.1 only and turn TLS off: the link is a
point-to-point cable, and the binary's self-signed certificate would stop a
Chromium --app window at a warning page. On any other address (Wi-Fi) it
listens on all interfaces with its default HTTPS.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.request
from pathlib import Path

from . import config
from .tablet_remote import REMOTE_WORK, USB_HOST, Remote, TabletError, q, tcp_open, wait_port

VERSION = "v1.4.0"
ASSET = "gomarkablestream-RMPRO"
URL = f"https://github.com/owulveryck/goMarkableStream/releases/download/{VERSION}/{ASSET}"
# Checked 2026-10-26 against both the GitHub API asset digest and the release's checksums.txt.
SHA256 = "401de5ca34116ca15db2e2710f5f433c37b7df84dd91db3891cfeea6ddf5c0e6"
PORT = 2001
REMOTE_BIN = f"{REMOTE_WORK}/gomarkablestream"
REMOTE_LOG = f"{REMOTE_WORK}/gomarkablestream.log"
REMOTE_PID = f"{REMOTE_WORK}/gomarkablestream.pid"


def local_binary() -> Path:
    return config.DATA_DIR / "bin" / f"{ASSET}-{VERSION}"


def ensure_binary(on_progress=None) -> Path:
    target = local_binary()
    if target.exists() and _sha256(target.read_bytes()) == SHA256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress("downloading goMarkableStream")
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(URL, timeout=120) as resp:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        digest = _sha256(Path(tmp).read_bytes())
        if digest != SHA256:
            raise TabletError(f"The goMarkableStream download didn't match its checksum ({digest[:12]}…), "
                              "so it wasn't used.")
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def credentials() -> dict:
    """Our own login for the stream page, created once and kept private."""
    path = config.CONFIG_DIR / "stream.json"
    try:
        creds = json.loads(path.read_text())
        if creds.get("username") and creds.get("password"):
            return creds
    except (OSError, ValueError):
        pass
    creds = {"username": "omarchy", "password": secrets.token_urlsafe(18)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)
    os.chmod(path, 0o600)
    return creds


def mode_for(host: str, tls: bool | None) -> tuple[bool, str]:
    """(use TLS, bind address) for a host."""
    on_usb = host == USB_HOST
    use_tls = (not on_usb) if tls is None else tls
    return use_tls, (f"{USB_HOST}:{PORT}" if on_usb else f":{PORT}")


def url_for(host: str, use_tls: bool) -> str:
    return f"{'https' if use_tls else 'http'}://{host}:{PORT}/"


_FIND = f"""
found=""
for d in /proc/[0-9]*; do
  exe=$(readlink "$d/exe" 2>/dev/null) || continue
  case "$exe" in {REMOTE_BIN}*) found="$found ${{d#/proc/}}";; esac
done
"""


def status(remote: Remote) -> dict:
    out = remote.run(_FIND + f'echo "pids:$found"; cat {q(REMOTE_PID)}.json 2>/dev/null', check=False).text
    pids = []
    meta: dict = {}
    for line in out.splitlines():
        if line.startswith("pids:"):
            pids = [int(p) for p in line[5:].split() if p.isdigit()]
        elif line.startswith("{"):
            try:
                meta = json.loads(line)
            except ValueError:
                pass
    running = bool(pids)
    use_tls = bool(meta.get("tls", remote.host != USB_HOST))
    result = {"running": running, "pids": pids, "host": remote.host, "port": PORT}
    if running:
        creds = credentials()
        result.update(url=url_for(remote.host, use_tls), tls=use_tls, username=creds["username"],
                      password=creds["password"], listening=tcp_open(remote.host, PORT, 1.0))
    return result


def deploy(remote: Remote, on_progress=None) -> bool:
    """Copy the binary over unless the tablet already has this exact one."""
    binary = ensure_binary(on_progress)
    data = binary.read_bytes()
    have = remote.run(f"md5sum {q(REMOTE_BIN)} 2>/dev/null", check=False).text.split()
    if have and have[0] == hashlib.md5(data).hexdigest():
        return False
    if on_progress:
        on_progress("copying goMarkableStream to the tablet")
    remote.put(data, REMOTE_BIN, mode=0o755, timeout=600)
    return True


def start(remote: Remote, *, tls: bool | None = None, on_progress=None) -> dict:
    current = status(remote)
    if current["running"]:
        return {**current, "started": False}
    deployed = deploy(remote, on_progress)
    use_tls, bind = mode_for(remote.host, tls)
    creds = credentials()
    meta = json.dumps({"tls": use_tls, "bind": bind, "started": time.time()})
    # The password travels on stdin so it never shows up in a process list here.
    script = (
        "IFS= read -r user; IFS= read -r pass\n"
        f"cd {q(REMOTE_WORK)}\n"
        f"RK_SERVER_USERNAME=\"$user\" RK_SERVER_PASSWORD=\"$pass\" RK_HTTPS={'true' if use_tls else 'false'} "
        f"RK_SERVER_BIND_ADDR={q(bind)} setsid nohup {q(REMOTE_BIN)} > {q(REMOTE_LOG)} 2>&1 < /dev/null &\n"
        f"echo $! > {q(REMOTE_PID)}\n"
        f"printf '%s\\n' {q(meta)} > {q(REMOTE_PID)}.json\n"
        "echo started"
    )
    remote.run(script, input=f"{creds['username']}\n{creds['password']}\n".encode(), timeout=30)
    if not wait_port(remote.host, PORT, timeout=15):
        log = remote.run(f"tail -n 15 {q(REMOTE_LOG)} 2>/dev/null", check=False).text.strip()
        stop(remote)
        raise TabletError("goMarkableStream didn't start on the tablet"
                          + (f": {log.splitlines()[-1]}" if log else "."))
    return {"running": True, "started": True, "deployed": deployed, "url": url_for(remote.host, use_tls),
            "tls": use_tls, "username": creds["username"], "password": creds["password"],
            "host": remote.host, "port": PORT}


def stop(remote: Remote) -> dict:
    """Kill only processes running our copy of the binary."""
    out = remote.run(_FIND + 'for p in $found; do kill "$p" 2>/dev/null; done\n'
                     f'rm -f {q(REMOTE_PID)} {q(REMOTE_PID)}.json\necho "killed:$found"', check=False).text
    killed = []
    for line in out.splitlines():
        if line.startswith("killed:"):
            killed = [int(p) for p in line[7:].split() if p.isdigit()]
    return {"running": False, "stopped": killed}
