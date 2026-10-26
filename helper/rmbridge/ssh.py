"""Developer-mode features over SSH: status, setup, device info, backups,
templates, sleep screen, live screen and the pen as a drawing tablet.

Daemon-facing functions: reachable(), info(), fetch_templates(). Commands are
registered under `ssh`. The heavy lifting lives in the tablet_* modules;
this file is the command surface plus device info.
"""

from __future__ import annotations

import argparse
import functools
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import config
from . import tablet_remote as tr
from .tablet_remote import PASSWORD_WHERE, WIFI_HINT, TabletError, q

XOCHITL_PARENT = "/home/root/.local/share/remarkable"
AUTH_CACHE_OK = 300  # seconds a successful key check is trusted while the port keeps answering
AUTH_CACHE_FAIL = 60
KEEP_BACKUPS = 5
MODELS = {
    "reMarkable Ferrari": "reMarkable Paper Pro",
    "reMarkable Chiappa": "reMarkable Paper Pro Move",
    "reMarkable Tatsu": "reMarkable Paper Pure",
    "reMarkable 2.0": "reMarkable 2",
    "reMarkable 1.0": "reMarkable 1",
}


def _ok(**kw) -> dict:
    return {"ok": True, **kw}


def _fail(error: str, code: str, **kw) -> dict:
    return {"ok": False, "error": error, "code": code, **kw}


def guarded(fn: Callable[[argparse.Namespace], dict]) -> Callable[[argparse.Namespace], dict]:
    """Report tablet and input problems as plain {"ok": false} results."""

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace) -> dict:
        try:
            return fn(args)
        except TabletError as e:
            return _fail(str(e), e.code)
        except (ValueError, FileNotFoundError, PermissionError) as e:
            return _fail(str(e), "invalid")
    return wrapper


# ================================================================ reachability


def _auth_cache() -> Path:
    return config.RUNTIME_DIR / "ssh-auth.json"


def _read_auth_cache(host: str, user: str) -> dict | None:
    try:
        c = json.loads(_auth_cache().read_text())
    except (OSError, ValueError):
        return None
    if c.get("host") != host or c.get("user") != user or c.get("key") != _key_stamp():
        return None
    return c


def _write_auth_cache(host: str, user: str, ok: bool) -> None:
    config.write_json_atomic(_auth_cache(), {"host": host, "user": user, "ok": ok, "at": time.time(),
                                             "key": _key_stamp()})


def _clear_auth_cache() -> None:
    _auth_cache().unlink(missing_ok=True)


def _key_stamp() -> float:
    try:
        return tr.key_path().stat().st_mtime
    except OSError:
        return 0.0


def reachable(timeout: float = 1.5) -> bool:
    """True when the tablet answers on port 22 and accepts our key.

    The port check is always live; the key check is cached so the daemon's
    polling doesn't open a new ssh session every time. Never takes much
    longer than `timeout`.
    """
    deadline = time.monotonic() + timeout
    host, user = tr.settings_target()
    if not tr.key_path().exists():
        return False
    if not tr.tcp_open(host, 22, timeout=min(1.0, timeout)):
        _clear_auth_cache()
        return False
    cached = _read_auth_cache(host, user)
    if cached:
        age = time.time() - cached.get("at", 0)
        if cached.get("ok") and age < AUTH_CACHE_OK:
            return True
        if not cached.get("ok") and age < AUTH_CACHE_FAIL:
            return False
    remaining = deadline - time.monotonic()
    if remaining < 0.3:
        return bool(cached and cached.get("ok"))
    try:
        tr.connect(host, user).run("true", timeout=remaining, connect_timeout=remaining)
        ok = True
    except tr.Unreachable:
        return False  # don't remember a timeout as a refusal
    except TabletError:
        ok = False
    _write_auth_cache(host, user, ok)
    return ok


# ================================================================ device info

INFO_SCRIPT = r"""
echo '@@machine'; cat /sys/devices/soc0/machine 2>/dev/null
echo '@@os-release'; cat /etc/os-release 2>/dev/null
echo '@@version'; cat /etc/version 2>/dev/null
echo '@@update.conf'; cat /usr/share/remarkable/update.conf 2>/dev/null
echo '@@hostname'; cat /proc/sys/kernel/hostname 2>/dev/null
echo '@@uptime'; cat /proc/uptime 2>/dev/null
echo '@@kernel'; uname -r 2>/dev/null
echo '@@df'; df -Pk /home 2>/dev/null
echo '@@power'
for d in /sys/class/power_supply/*; do
  [ -d "$d" ] || continue
  echo "[${d##*/}]"
  for f in type status capacity capacity_level temp present online health cycle_count; do
    [ -r "$d/$f" ] && echo "$f=$(cat "$d/$f" 2>/dev/null)"
  done
done
echo '@@net'; ip -o -4 addr show 2>/dev/null
echo '@@wifissh'; if [ -e /data/internal/rm_enable_ssh_wifi_marker ]; then echo on; else echo off; fi
echo '@@end'
"""


def _sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("@@"):
            if current:
                out[current] = "\n".join(lines).strip()
            current, lines = line[2:].strip(), []
        else:
            lines.append(line)
    if current:
        out[current] = "\n".join(lines).strip()
    return out


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_power(text: str) -> dict:
    supplies: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            supplies[current] = {}
        elif current and "=" in line:
            k, v = line.split("=", 1)
            supplies[current][k] = v.strip()
    batteries = [(n, s) for n, s in supplies.items()
                 if s.get("type", "").lower() == "battery" or "battery" in n.lower()]
    # Prefer a gauge with a capacity reading (Paper Pro: max1726x_battery).
    batteries.sort(key=lambda b: (b[1].get("capacity") is None, b[0]))
    result: dict = {"supplies": sorted(supplies)}
    if batteries:
        name, b = batteries[0]
        status = b.get("status", "")
        temp = _int(b.get("temp"))
        result.update(source=name, percent=_int(b.get("capacity")), status=status or None,
                      charging=status.lower() == "charging", full=status.lower() == "full",
                      health=b.get("health"), cycleCount=_int(b.get("cycle_count")))
        if temp is not None:
            # power_supply reports tenths of a degree; a few drivers use millidegrees.
            result["temperatureC"] = round(temp / 1000 if abs(temp) > 1500 else temp / 10, 1)
    plugged = any(s.get("online") == "1" for n, s in supplies.items() if (n, s) not in batteries)
    result["pluggedIn"] = plugged or result.get("status", "") in ("Charging", "Full", "Not charging")
    return result


def parse_df(text: str) -> dict | None:
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[1].isdigit():
            total, used, free = (int(parts[i]) * 1024 for i in (1, 2, 3))
            return {"path": parts[5], "totalBytes": total, "usedBytes": used, "freeBytes": free,
                    "usedPercent": round(100 * used / total, 1) if total else None}
    return None


def parse_addresses(text: str) -> dict[str, str]:
    addrs: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/\d+", line.strip())
        if m and m.group(1) != "lo":
            addrs[m.group(1)] = m.group(2)
    return addrs


def parse_info(text: str) -> dict:
    from .tablet_system import parse_firmware

    s = _sections(text)
    machine = s.get("machine", "").strip()
    os_release = dict(l.split("=", 1) for l in s.get("os-release", "").splitlines() if "=" in l)
    fw = parse_firmware(f"{s.get('os-release', '')}@@{s.get('update.conf', '')}@@{s.get('version', '')}")
    uptime = None
    try:
        uptime = int(float(s.get("uptime", "").split()[0]))
    except (IndexError, ValueError):
        pass
    power = parse_power(s.get("power", ""))
    storage = parse_df(s.get("df", ""))
    addrs = parse_addresses(s.get("net", ""))
    wifi = next((ip for name, ip in addrs.items() if name.startswith(("wlan", "wl"))), None)
    usb_ip = next((ip for name, ip in addrs.items() if name.startswith("usb")), None)
    return {
        "model": MODELS.get(machine, machine or "reMarkable"), "machine": machine or None,
        "firmware": fw, "build": s.get("version") or None,
        "os": os_release.get("PRETTY_NAME", os_release.get("NAME", "")).strip('"') or None,
        "kernel": s.get("kernel") or None, "hostname": s.get("hostname") or None,
        "uptime": uptime,
        "battery": power.get("percent"), "charging": bool(power.get("charging")),
        "pluggedIn": bool(power.get("pluggedIn")), "temperature": power.get("temperatureC"),
        "batteryStatus": power.get("status"), "batteryHealth": power.get("health"),
        "batterySource": power.get("source"),
        "storageFree": storage["freeBytes"] if storage else None,
        "storageTotal": storage["totalBytes"] if storage else None,
        "storageUsed": storage["usedBytes"] if storage else None,
        "wifiIp": wifi, "usbIp": usb_ip, "addresses": addrs,
        "wifiSsh": s.get("wifissh", "").strip() == "on",
    }


def info() -> dict:
    """Battery, storage, firmware and network facts from the tablet (one ssh call)."""
    remote = tr.connect()
    data = parse_info(remote.run(INFO_SCRIPT, timeout=20, check=False).text)
    data["host"] = remote.host
    return data


def fetch_templates() -> dict:
    """Copy the tablet's templates into the render cache (the daemon calls this once)."""
    from . import tablet_templates
    return tablet_templates.fetch(tr.connect())


# ================================================================ status / setup


@guarded
def cmd_status(args: argparse.Namespace) -> dict:
    host, user = tr.settings_target()
    key = tr.key_path().exists()
    port = tr.tcp_open(host, 22, timeout=1.5)
    result = {"host": host, "user": user, "keyCreated": key, "reachable": port, "authorized": False}
    if port and key:
        try:
            tr.connect(host, user).run("true", timeout=10)
            result["authorized"] = True
            _write_auth_cache(host, user, True)
        except TabletError as e:
            result["problem"] = str(e)
            result["problemCode"] = e.code
    if not port:
        result["hint"] = (f"The tablet isn't answering at {host}. Plug it in by USB-C and wake it "
                          f"(developer mode must be on). {WIFI_HINT}")
    elif not result["authorized"]:
        result["hint"] = (f"Run setup with the root password from {PASSWORD_WHERE} on the tablet.")
    result["setupNeeded"] = port and not result["authorized"]
    return _ok(**result)


INSTALL_KEY = r"""
umask 077
f=/home/root/.ssh/authorized_keys
mkdir -p /home/root/.ssh
touch "$f"
if [ -s "$f" ] && [ "$(tail -c 1 "$f" | wc -l)" -eq 0 ]; then echo >> "$f"; fi
grep -qxF {key} "$f" || printf '%s\n' {key} >> "$f"
chmod 700 /home/root/.ssh
chmod 600 "$f"
echo key-installed
"""


def _read_password() -> str:
    if sys.stdin.isatty():
        return getpass.getpass(f"Root password ({PASSWORD_WHERE}): ")
    return sys.stdin.readline().rstrip("\r\n")


@guarded
def cmd_setup(args: argparse.Namespace) -> dict:
    host, user = tr.settings_target()
    password = _read_password()
    if not password:
        return _fail(f"No password given. It's on the tablet under {PASSWORD_WHERE}.", "invalid")
    if not tr.tcp_open(host, 22, timeout=3):
        return _fail(f"The tablet isn't answering at {host}. Plug it in by USB-C and wake it "
                     "(developer mode must be on).", "unreachable")
    if args.reset_host_key and tr.known_hosts().exists():
        subprocess.run(["ssh-keygen", "-R", host, "-f", str(tr.known_hosts())],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    pub = tr.public_key()
    remote = tr.connect(host, user)
    askpass = config.RUNTIME_DIR / "askpass.sh"
    askpass.parent.mkdir(parents=True, exist_ok=True)
    # The script holds no secret; it echoes an environment variable only ssh's child sees.
    askpass.write_text('#!/bin/sh\nprintf \'%s\\n\' "$OMARCHY_REMARKABLE_PW"\n')
    askpass.chmod(0o700)
    env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force",
               DISPLAY=os.environ.get("DISPLAY", ":0"), OMARCHY_REMARKABLE_PW=password)
    cmd = remote.ssh_args(batch=False, connect_timeout=10) + [
        "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "PubkeyAuthentication=no",
        "-o", "NumberOfPasswordPrompts=1", remote.target, INSTALL_KEY.format(key=q(pub))]
    try:
        proc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                              start_new_session=True)
    except subprocess.TimeoutExpired:
        return _fail(f"The tablet at {host} stopped answering during setup.", "unreachable")
    finally:
        askpass.unlink(missing_ok=True)
    err = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0 or b"key-installed" not in proc.stdout:
        e = tr.classify(err, host)
        if isinstance(e, tr.AuthFailed):
            return _fail(f"The tablet didn't accept that password. Check it under {PASSWORD_WHERE} "
                         "(it changes after a factory reset).", "auth")
        return _fail(str(e), e.code)
    remote.run("true", timeout=15)
    _write_auth_cache(host, user, True)
    fingerprint = subprocess.run(["ssh-keygen", "-lf", str(tr.key_path().with_suffix(".pub"))],
                                 capture_output=True, text=True, timeout=10).stdout.split()
    return _ok(host=host, user=user, keyInstalled=True,
               fingerprint=fingerprint[1] if len(fingerprint) > 1 else None)


@guarded
def cmd_info(args: argparse.Namespace) -> dict:
    return _ok(**info())


# ================================================================ backup


def backup(dest: Path | None = None, *, keep: int = KEEP_BACKUPS, on_progress=None) -> dict:
    """Stream the whole xochitl folder as a zstd-compressed tar, keeping the newest `keep`."""
    remote = tr.connect()
    folder = dest or config.setting_path(config.load_settings(), "backupDir") / "ssh"
    folder.mkdir(parents=True, exist_ok=True)
    du = remote.run(f"du -sk {q(XOCHITL_PARENT)}/xochitl", check=False).text.split()
    total = int(du[0]) * 1024 if du and du[0].isdigit() else None
    name = time.strftime("xochitl-%Y%m%d-%H%M%S.tar.zst")
    target = folder / name
    partial = folder / (name + ".partial")
    errlog = config.TMP_DIR / f"ssh-backup-{os.getpid()}.err"
    errlog.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    try:
        with open(partial, "wb") as out, open(errlog, "wb") as err:
            zstd = subprocess.Popen(["zstd", "-q", "-T0", "-3", "-c"], stdin=subprocess.PIPE, stdout=out)
            proc = remote.popen(f"tar -C {q(XOCHITL_PARENT)} -cf - xochitl", stderr=err)
            last = 0.0
            assert proc.stdout is not None and zstd.stdin is not None
            while chunk := proc.stdout.read(1 << 20):
                zstd.stdin.write(chunk)
                done += len(chunk)
                if on_progress and time.monotonic() - last > 1:
                    last = time.monotonic()
                    on_progress("backing up", done, total)
            zstd.stdin.close()
            rc, zrc = proc.wait(), zstd.wait()
        err_text = errlog.read_text(errors="replace").strip()
        if rc == 255:
            raise tr.classify(err_text, remote.host)
        if zrc != 0:
            raise TabletError("Compressing the backup failed (is zstd installed?).")
        check = subprocess.run(f"zstd -dc {q(str(partial))} | tar -tf - > /dev/null", shell=True,
                               capture_output=True, timeout=600)
        if check.returncode != 0 or done == 0:
            raise TabletError("The backup came out incomplete"
                              + (f": {err_text.splitlines()[-1]}" if err_text else "") + "; nothing was kept.")
        partial.rename(target)
    finally:
        partial.unlink(missing_ok=True)
        errlog.unlink(missing_ok=True)
    old = sorted(folder.glob("xochitl-*.tar.zst"))[:-keep] if keep > 0 else []
    for f in old:
        f.unlink(missing_ok=True)
    return {"path": str(target), "bytes": target.stat().st_size, "uncompressed": done,
            "removed": [str(f) for f in old], "kept": keep}


@guarded
def cmd_backup(args: argparse.Namespace) -> dict:
    from .cli import progress
    dest = Path(args.dest).expanduser() if args.dest else None
    return _ok(**backup(dest, keep=args.keep, on_progress=progress))


# ================================================================ templates


@guarded
def cmd_templates_fetch(args: argparse.Namespace) -> dict:
    return _ok(**fetch_templates())


@guarded
def cmd_templates_list(args: argparse.Namespace) -> dict:
    from . import tablet_templates as tt
    if not args.cached:
        try:
            return _ok(cached=False, **tt.list_live(tr.connect()))
        except tr.Unreachable:
            pass
    index = tt.cached_index()
    if index is None:
        return _fail("The tablet isn't reachable and no templates have been fetched yet.", "unreachable")
    return _ok(cached=True, firmware=index.get("firmware"), templates=index.get("templates", []),
               fetched=index.get("fetched"))


def _confirm(action: str, plan: list[str], **extra) -> dict:
    from .tablet_system import FIRMWARE_WARNING
    return _fail(f"This changes the tablet's system files ({action}). Run it again with --yes to go ahead.",
                 "needs-confirmation", plan=plan, warning=FIRMWARE_WARNING, **extra)


@guarded
def cmd_templates_install(args: argparse.Namespace) -> dict:
    from . import tablet_templates as tt
    src = Path(args.file).expanduser()
    landscape = True if args.landscape else (False if args.portrait else None)
    prepared = tt.prepare_install(src, args.name, args.category, landscape)
    entry = prepared["entry"]
    plan = [f"add “{entry['name']}” ({'landscape' if entry.get('landscape') else 'portrait'}, "
            f"{args.category}) to the template list",
            *[f"copy {n} to {tt.TEMPLATES_DIR}" for n in sorted(prepared["files"])],
            *prepared["notes"],
            "back up the original templates.json first",
            "restart the tablet's interface so the template shows up" if not args.no_restart else
            "don't restart the interface (the template appears after the next restart)"]
    if not args.yes:
        return _confirm("adds a template", plan)
    return _ok(**tt.install(tr.connect(), prepared, restart=not args.no_restart), notes=prepared["notes"])


@guarded
def cmd_templates_remove(args: argparse.Namespace) -> dict:
    from . import tablet_templates as tt
    if not args.yes:
        return _confirm("removes a template", [f"remove “{args.name}” from the template list and delete its files",
                                               "only templates added after the tablet shipped can be removed"])
    return _ok(**tt.remove(tr.connect(), args.name, restart=not args.no_restart))


# ================================================================ sleep screen


@guarded
def cmd_sleep_status(args: argparse.Namespace) -> dict:
    from . import tablet_system as ts
    return _ok(**ts.sleepscreen_status(tr.connect()))


@guarded
def cmd_sleep_set(args: argparse.Namespace) -> dict:
    from . import tablet_system as ts
    image = Path(args.image).expanduser()
    if not image.exists():
        return _fail(f"{image} doesn't exist", "invalid")
    data, conv, plan = ts.sleepscreen_plan(image, args.carousel)
    if not args.yes:
        return _confirm("replaces the sleep screen", plan)
    result = ts.sleepscreen_set(tr.connect(), data, carousel=args.carousel, restart=args.restart)
    note = None if args.restart else "If the tablet still shows the old picture, run this again with --restart."
    return _ok(**result, image=conv, warning=ts.FIRMWARE_WARNING, note=note)


@guarded
def cmd_sleep_restore(args: argparse.Namespace) -> dict:
    from . import tablet_system as ts
    if not args.yes:
        return _confirm("puts back the original sleep screen", ["copy the backed-up stock sleep screen back"])
    return _ok(**ts.sleepscreen_restore(tr.connect(), restart=args.restart))


# ================================================================ live screen


@guarded
def cmd_stream_start(args: argparse.Namespace) -> dict:
    from . import tablet_stream
    from .cli import progress
    tls = True if args.tls else (False if args.no_tls else None)
    return _ok(**tablet_stream.start(tr.connect(), tls=tls, on_progress=progress))


@guarded
def cmd_stream_stop(args: argparse.Namespace) -> dict:
    from . import tablet_stream
    return _ok(**tablet_stream.stop(tr.connect()))


def cmd_stream_status(args: argparse.Namespace) -> dict:
    from . import tablet_stream
    host, _ = tr.settings_target()
    if not tr.tcp_open(host, 22, timeout=1.5):
        return _ok(running=False, reachable=False, host=host)
    try:
        return _ok(reachable=True, **tablet_stream.status(tr.connect()))
    except TabletError as e:
        return _ok(running=False, reachable=False, host=host, problem=str(e))


# ================================================================ pen


def _pen_file() -> Path:
    return config.RUNTIME_DIR / "pen.json"


def _pen_state() -> dict | None:
    try:
        state = json.loads(_pen_file().read_text())
    except (OSError, ValueError):
        return None
    pid = state.get("pid")
    if not isinstance(pid, int) or not _alive(pid):
        return None
    return state


def _alive(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"rmbridge" in cmdline and b"pen" in cmdline


def _input_device_present(name: str) -> bool:
    try:
        return f'N: Name="{name}"' in Path("/proc/bus/input/devices").read_text()
    except OSError:
        return False


class _Stop(Exception):
    pass


def cmd_pen_start(args: argparse.Namespace) -> dict:
    from . import tablet_pen as tp

    args.streaming = True

    def emit(**kw) -> None:
        print(json.dumps(kw), flush=True)

    def fail(error: str, code: str) -> dict:
        result = _fail(error, code)
        emit(**result)
        return result

    if _pen_state():
        return fail("The pen is already connected; stop it first.", "busy")
    try:
        remote = tr.connect()
        devices = tp.parse_input_devices(remote.run("cat /proc/bus/input/devices", timeout=15).text)
    except TabletError as e:
        return fail(str(e), e.code)
    dev = tp.find_pen(devices)
    if dev is None or not dev.event_node:
        return fail("Couldn't find the pen digitizer on the tablet.", "no-pen")
    profile = tp.profile_for(dev)
    for code, value in ((tp.ABS_X, args.max_x), (tp.ABS_Y, args.max_y), (tp.ABS_PRESSURE, args.max_pressure)):
        if value:
            profile.axes[code] = tp.Axis(profile.axes[code].minimum, value, profile.axes[code].resolution)
    mapper = tp.Mapper(profile, args.orientation)
    relative = args.mode == "relative"
    translator = tp.Translator(mapper, relative=relative, speed=args.speed)

    def on_signal(signum, frame):
        raise _Stop(signal.Signals(signum).name)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)
    proc = None
    remote_pids: list[int] = []
    reason = "tablet closed the connection"
    errors: list[str] = []
    try:
        with tp.VirtualTablet(mapper.output_axes(profile), relative=relative) as vt:
            proc = remote.popen(tp.remote_stream_script(dev.event_node))
            state = {"pid": os.getpid(), "sshPid": proc.pid, "host": remote.host, "node": dev.event_node,
                     "digitizer": dev.name, "device": vt.name, "local": vt.event_node(),
                     "orientation": args.orientation, "mode": args.mode, "started": time.time(), "remotePids": []}
            config.write_json_atomic(_pen_file(), state)

            def watch_stderr() -> None:
                assert proc is not None and proc.stderr is not None
                for raw in iter(proc.stderr.readline, b""):
                    line = raw.decode("utf-8", "replace").strip(". \n")
                    if line.startswith("pids "):
                        remote_pids[:] = [int(p) for p in line.split()[1:] if p.isdigit()]
                        state["remotePids"] = remote_pids
                        config.write_json_atomic(_pen_file(), state)
                    elif line:
                        errors.append(line)

            threading.Thread(target=watch_stderr, daemon=True).start()
            emit(event="started", ok=True, device=vt.name, local=vt.event_node(), digitizer=dev.name,
                 remoteNode=dev.event_node, orientation=args.orientation, mode=args.mode,
                 axes={str(k): [a.minimum, a.maximum] for k, a in mapper.output_axes(profile).items()},
                 hyprland=tp.hyprland_hint(vt.name),
                 note="The tablet still draws on its own screen while you use the pen here; "
                      "a blank notebook page works best.")
            tool = None
            assert proc.stdout is not None
            for etype, code, value in tp.read_events(proc.stdout):
                vt.emit(translator.feed(etype, code, value))
                if translator.tool != tool:
                    tool = translator.tool
                    emit(event="tool", tool=tool)
            rc = proc.wait()
            if rc == 255 and errors:
                reason = str(tr.classify("\n".join(errors), remote.host))
    except _Stop as e:
        reason = f"stopped ({e})"
    except (PermissionError, FileNotFoundError) as e:
        return fail(str(e), "uinput")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        _pen_file().unlink(missing_ok=True)
        try:
            remote.run(tp.remote_cleanup_script(remote_pids[1:]), timeout=5, check=False, connect_timeout=3)
        except TabletError:
            pass  # the wake lock lapses on its own
    emit(event="stopped", ok=True, reason=reason, events=translator.events)
    return _ok(stopped=True, reason=reason)


def cmd_pen_stop(args: argparse.Namespace) -> dict:
    state = _pen_state()
    if not state:
        _pen_file().unlink(missing_ok=True)
        return _ok(running=False, stopped=False)
    pid = state["pid"]
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not _alive(pid):
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        _pen_file().unlink(missing_ok=True)
        try:
            from . import tablet_pen as tp
            tr.connect().run(tp.remote_cleanup_script(state.get("remotePids", [])[1:]), timeout=5,
                             check=False, connect_timeout=3)
        except TabletError:
            pass
    return _ok(running=False, stopped=True)


def cmd_pen_status(args: argparse.Namespace) -> dict:
    from . import tablet_pen as tp
    state = _pen_state()
    if not state:
        return _ok(running=False)
    return _ok(running=True, device=state.get("device"), local=state.get("local"),
               orientation=state.get("orientation"), mode=state.get("mode"), started=state.get("started"),
               host=state.get("host"), devicePresent=_input_device_present(state.get("device") or tp.DEVICE_NAME))


# ================================================================ register


def register(sub) -> None:
    p = sub.add_parser("ssh", help="developer-mode features over SSH")
    s = p.add_subparsers(dest="action", required=True)

    x = s.add_parser("status", help="is the tablet reachable and set up")
    x.set_defaults(func=cmd_status)
    x = s.add_parser("setup", help="install our key (root password on stdin)")
    x.add_argument("--reset-host-key", action="store_true", help="forget the tablet's old identity first")
    x.set_defaults(func=cmd_setup)
    x = s.add_parser("info", help="battery, storage, firmware, network")
    x.set_defaults(func=cmd_info)
    x = s.add_parser("backup", help="full backup of the notebooks folder (tar.zst)")
    x.add_argument("--dest")
    x.add_argument("--keep", type=int, default=KEEP_BACKUPS)
    x.set_defaults(func=cmd_backup)

    t = s.add_parser("templates", help="page templates").add_subparsers(dest="templates_action", required=True)
    x = t.add_parser("fetch", help="copy the tablet's templates for rendering")
    x.set_defaults(func=cmd_templates_fetch)
    x = t.add_parser("list")
    x.add_argument("--cached", action="store_true")
    x.set_defaults(func=cmd_templates_list)
    x = t.add_parser("install", help="add an SVG/PNG/.template as a template")
    x.add_argument("file")
    x.add_argument("--name", required=True)
    x.add_argument("--category", default="Creative", help="Creative, Grids, Lines or Planners")
    o = x.add_mutually_exclusive_group()
    o.add_argument("--landscape", action="store_true")
    o.add_argument("--portrait", action="store_true")
    x.add_argument("--no-restart", action="store_true", help="don't restart the tablet's interface")
    x.add_argument("--yes", action="store_true")
    x.set_defaults(func=cmd_templates_install)
    x = t.add_parser("remove", help="remove a template added earlier")
    x.add_argument("name")
    x.add_argument("--no-restart", action="store_true")
    x.add_argument("--yes", action="store_true")
    x.set_defaults(func=cmd_templates_remove)

    z = s.add_parser("sleepscreen", help="the picture shown while asleep").add_subparsers(
        dest="sleep_action", required=True)
    x = z.add_parser("status")
    x.set_defaults(func=cmd_sleep_status)
    x = z.add_parser("set")
    x.add_argument("image")
    x.add_argument("--carousel", action="store_true", help="also replace the sleep illustrations")
    x.add_argument("--restart", action="store_true", help="restart the tablet's interface afterwards")
    x.add_argument("--yes", action="store_true")
    x.set_defaults(func=cmd_sleep_set)
    x = z.add_parser("restore")
    x.add_argument("--restart", action="store_true")
    x.add_argument("--yes", action="store_true")
    x.set_defaults(func=cmd_sleep_restore)

    v = s.add_parser("stream", help="live view of the tablet's screen").add_subparsers(
        dest="stream_action", required=True)
    x = v.add_parser("start")
    o = x.add_mutually_exclusive_group()
    o.add_argument("--tls", action="store_true", help="force HTTPS")
    o.add_argument("--no-tls", action="store_true", help="force plain HTTP")
    x.set_defaults(func=cmd_stream_start)
    x = v.add_parser("stop")
    x.set_defaults(func=cmd_stream_stop)
    x = v.add_parser("status")
    x.set_defaults(func=cmd_stream_status)

    from .tablet_pen import ORIENTATIONS
    n = s.add_parser("pen", help="use the Marker as a drawing tablet").add_subparsers(
        dest="pen_action", required=True)
    x = n.add_parser("start", help="runs in the foreground, one JSON line per event")
    x.add_argument("--orientation", choices=ORIENTATIONS, default="portrait")
    x.add_argument("--mode", choices=["absolute", "relative"], default="absolute")
    x.add_argument("--speed", type=float, default=1.0, help="cursor speed in relative mode")
    x.add_argument("--max-x", type=int, help="override the digitizer's X range")
    x.add_argument("--max-y", type=int, help="override the digitizer's Y range")
    x.add_argument("--max-pressure", type=int, help="override the pressure range")
    x.set_defaults(func=cmd_pen_start)
    x = n.add_parser("stop")
    x.set_defaults(func=cmd_pen_stop)
    x = n.add_parser("status")
    x.set_defaults(func=cmd_pen_status)
