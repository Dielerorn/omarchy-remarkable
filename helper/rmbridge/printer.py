"""A "reMarkable" printer on this computer, no root needed.

CUPS's ippeveprinter runs as the user and advertises an IPP Everywhere printer
over DNS-SD (through the running avahi-daemon), so print dialogs list it
without any setup. It only accepts PDF, which is what GTK, Chrome and CUPS
send to driverless PDF printers. For each job it runs `printer job <file>`
(through a small wrapper script, since -c takes a bare path), which uploads
the file to settings["printFolder"].

Testing overrides (set in serve's environment, inherited by every job):
  OMARCHY_REMARKABLE_PRINT_FOLDER=F   upload into cloud folder F instead
  OMARCHY_REMARKABLE_PRINT_DRYRUN=DIR copy job files into DIR, no upload
`printer serve --folder F` and `--dry-run DIR` set them.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

NAME = "reMarkable"  # the DNS-SD service name print dialogs show
QUEUE = "reMarkable"  # persistent CUPS queue name for `printer install`
DEFAULT_PORT = 8631  # next to CUPS's 631; fixed so an installed queue keeps working
PORT_TRIES = 10
FOLDER_ENV = "OMARCHY_REMARKABLE_PRINT_FOLDER"
DRYRUN_ENV = "OMARCHY_REMARKABLE_PRINT_DRYRUN"

PRINTER_DIR = config.CACHE_DIR / "printer"
SPOOL_DIR = PRINTER_DIR / "spool"
JOBS_LOG = PRINTER_DIR / "jobs.jsonl"
STATE_FILE = config.RUNTIME_DIR / "printer.json"
LOCK_FILE = config.RUNTIME_DIR / "printer.lock"
WRAPPER = config.RUNTIME_DIR / "printer-job"

SUFFIXES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "text/plain": ".txt"}
NAME_PREFIXES = ("Microsoft Word - ", "Microsoft PowerPoint - ", "Microsoft Excel - ")
EXTENSIONS = re.compile(r"\.(pdf|docx?|odt|xlsx?|ods|pptx?|odp|txt|md|html?|rtf|epub|png|jpe?g)$", re.I)


class PrinterError(RuntimeError):
    pass


def ippeveprinter() -> str | None:
    return shutil.which("ippeveprinter")


def launcher() -> list[str]:
    """argv prefix that runs this helper, for the daemon to spawn."""
    plugin = os.environ.get("OMARCHY_REMARKABLE_PLUGIN")
    root = Path(plugin) if plugin else Path(__file__).resolve().parents[2]
    script = root / "bin" / "omarchy-remarkable"
    if script.exists():
        return [str(script)]
    return [sys.executable, "-m", "rmbridge"]


# -------------------------------------------------------------------- jobs


def job_title(job_name: str, filename: str = "", now: datetime | None = None) -> str:
    """A document name from the job name the app sent (Chrome sends the page
    title, GTK apps the document name, lp the file name)."""
    name = " ".join((job_name or "").split())
    for prefix in NAME_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    name = EXTENSIONS.sub("", name).strip()
    if not name or name.lower() in ("untitled", "untitled document", "(stdin)", "stdin", "-"):
        # ippeveprinter names spool files "<job id>-<job name>.<ext>".
        stem = re.sub(r"^\d+-", "", Path(filename).stem) if filename else ""
        name = stem if stem and stem.lower() not in ("untitled", "stdin") else ""
    if not name:
        name = f"Printed {(now or datetime.now()):%Y-%m-%d %H.%M}"
    return name[:120]


def _sniff_suffix(path: Path, content_type: str) -> str:
    if content_type in SUFFIXES:
        return SUFFIXES[content_type]
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\xff\xd8"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    raise PrinterError(f"can't send {content_type or 'this kind of'} print jobs; print as PDF")


def _record(entry: dict) -> None:
    """Keep the last few jobs for `printer status`."""
    PRINTER_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lines = JOBS_LOG.read_text().splitlines()[-49:]
    except OSError:
        lines = []
    lines.append(json.dumps(entry, default=str))
    tmp = JOBS_LOG.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(JOBS_LOG)


def last_jobs(n: int = 1) -> list[dict]:
    try:
        lines = JOBS_LOG.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def handle_job(path: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Upload one print job (or copy it, in a dry run)."""
    import tempfile

    from . import library, send

    env = dict(os.environ if env is None else env)
    if not path.exists():
        raise PrinterError(f"the print job file {path.name} is missing")
    content_type = env.get("CONTENT_TYPE", "")
    title = job_title(env.get("IPP_JOB_NAME", ""), str(path))
    suffix = _sniff_suffix(path, content_type)
    folder = env.get(FOLDER_ENV)
    if folder is None:
        folder = config.load_settings().get("printFolder", "")
    result: dict[str, Any] = {"job": env.get("IPP_JOB_ID"), "jobName": env.get("IPP_JOB_NAME", ""),
                              "user": env.get("IPP_JOB_ORIGINATING_USER_NAME", ""), "format": content_type,
                              "size": path.stat().st_size, "time": time.time()}
    dry = env.get(DRYRUN_ENV)
    if dry:
        target_dir = Path(dry).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (library.safe_name(title) + suffix)
        shutil.copyfile(path, target)
        result.update(name=title, folder=folder, dryRun=True, file=str(target))
        return result
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as tmp:
        # rmapi infers the type from the extension; spool names may lack one.
        staged = Path(tmp) / (library.safe_name(title) + suffix)
        shutil.copyfile(path, staged)
        from .web import unique_name
        name = unique_name(library.safe_name(title), folder)
        sent = send.send_file(staged, folder, name)
    library.fetch_library()
    result.update(name=sent["name"], id=sent["id"], folder=folder, dryRun=False)
    return result


def cmd_job(args: argparse.Namespace) -> dict:
    """What ippeveprinter runs for each job. Lines on stderr starting with
    INFO:/ERROR: become the job's status message in CUPS."""
    from . import cloud
    from .cli import notify

    config.ensure_dirs()
    try:
        result = handle_job(Path(args.file))
    except (PrinterError, cloud.CloudError, OSError, ValueError) as e:
        message = str(e)
        print(f"ERROR: {message}", file=sys.stderr, flush=True)
        _record({"ok": False, "error": message, "job": os.environ.get("IPP_JOB_ID"),
                 "jobName": os.environ.get("IPP_JOB_NAME", ""), "time": time.time()})
        notify("Couldn't print to reMarkable", message, icon="printer", urgency="critical")
        return {"ok": False, "error": message, "code": "printer"}
    _record({"ok": True, **result})
    where = f" ({result['file']})" if result.get("dryRun") else ""
    print(f"INFO: Sent to reMarkable as {result['name']}{where}", file=sys.stderr, flush=True)
    if not result.get("dryRun"):
        notify("Printed to reMarkable", result["name"], icon="printer")
    return {"ok": True, **result}


# ------------------------------------------------------------------ server


def port_free(port: int) -> bool:
    for family, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((addr, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def pick_port(wanted: int | None) -> int:
    if wanted:
        if not port_free(wanted):
            raise PrinterError(f"port {wanted} is already in use")
        return wanted
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES):
        if port_free(port):
            return port
    raise PrinterError(f"ports {DEFAULT_PORT}-{DEFAULT_PORT + PORT_TRIES - 1} are all in use")


def write_wrapper() -> Path:
    """ippeveprinter's -c takes a bare executable path and passes the job
    file as $1, so point it at a script that calls `printer job`."""
    helper = Path(__file__).resolve().parents[1]
    script = (
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(str(helper))}\n"
        f"export PYTHONPYCACHEPREFIX={shlex.quote(str(config.CACHE_DIR / 'pycache'))}\n"
        f'exec {shlex.quote(sys.executable)} -m rmbridge printer job "$1"\n'
    )
    WRAPPER.parent.mkdir(parents=True, exist_ok=True)
    tmp = WRAPPER.with_suffix(".tmp")
    tmp.write_text(script)
    tmp.chmod(0o755)
    tmp.replace(WRAPPER)
    return WRAPPER


def clean_spool(older_than: float = 3600) -> None:
    cutoff = time.time() - older_than
    for f in SPOOL_DIR.glob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def ipp_command(port: int, name: str = NAME) -> list[str]:
    exe = ippeveprinter()
    if not exe:
        raise PrinterError("ippeveprinter isn't installed (it comes with CUPS)")
    return [exe, "-v", "-p", str(port), "-d", str(SPOOL_DIR), "-f", "application/pdf",
            "-c", str(WRAPPER), "-M", "reMarkable", "-m", "Paper Pro", "-l", "Omarchy", name]


def _die_with_parent() -> None:
    # If serve is killed outright, take ippeveprinter (and the advert) with it.
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


_JOB_LINE = re.compile(r"^\[Job (\d+)\] (.*)$")


def parse_log(line: str) -> dict | None:
    """ippeveprinter's -v log line -> a JSON event, or None for chatter."""
    line = line.rstrip()
    m = _JOB_LINE.match(line)
    if m:
        job, text = int(m.group(1)), m.group(2)
        if text.startswith("Command - "):
            msg = text[len("Command - "):]
            if msg.startswith("ERROR:"):
                return {"event": "job-error", "job": job, "message": msg[6:].strip()}
            if msg.startswith("INFO:"):
                return {"event": "job-info", "job": job, "message": msg[5:].strip()}
            return None  # progress lines and other output from the job
        if "completed successfully" in text:
            return {"event": "job-done", "job": job}
        if "exited with status" in text or "terminated with signal" in text or "Unable to" in text:
            return {"event": "job-failed", "job": job, "message": text}
        return None
    m = re.search(r"Avahi state (\d+)", line)
    if m:
        # AvahiEntryGroupState: 2 established, 3 name collision, 4 failure.
        dnssd = {"2": "registered", "3": "collision", "4": "failed"}.get(m.group(1))
        return {"event": "dnssd", "state": dnssd} if dnssd else None
    m = re.match(r'^Created job file "(.*)", format "(.*)"\.$', line)
    if m:
        return {"event": "job-received", "file": m.group(1), "format": m.group(2)}
    if re.search(r"unable|error|failed|cannot|can't", line, re.I) and "Client closed" not in line:
        return {"event": "log", "message": line}
    return None


def _emit(event: dict) -> None:
    event.setdefault("time", round(time.time(), 3))
    print(json.dumps(event, default=str), flush=True)


def cmd_serve(args: argparse.Namespace) -> dict | None:
    """Run the printer in the foreground; one JSON line per event."""
    config.ensure_dirs()
    args.streaming = True
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        running = read_state()
        _emit({"event": "error", "ok": False,
               "error": f"the reMarkable printer is already running (port {running.get('port', '?')})"})
        return {"ok": False}
    try:
        port = pick_port(args.port)
        SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        clean_spool(0)
        write_wrapper()
        cmd = ipp_command(port, args.name)
    except PrinterError as e:
        _emit({"event": "error", "ok": False, "error": str(e)})
        return {"ok": False}

    env = dict(os.environ)
    if args.folder is not None:
        env[FOLDER_ENV] = args.folder
    if args.dry_run:
        env[DRYRUN_ENV] = str(Path(args.dry_run).expanduser().resolve())
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=env, preexec_fn=_die_with_parent)

    def stop(signum, frame):  # noqa: ARG001
        if proc.poll() is None:
            proc.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    state = {"pid": os.getpid(), "ippPid": proc.pid, "port": port, "name": args.name,
             "url": f"ipp://localhost:{port}/ipp/print", "started": time.time(),
             "dryRun": env.get(DRYRUN_ENV), "folder": env.get(FOLDER_ENV)}
    config.write_json_atomic(STATE_FILE, state)
    _emit({"event": "started", **state})
    last_error = ""
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            event = parse_log(line)
            if args.verbose and event is None:
                event = {"event": "log", "message": line.rstrip()}
            if event is None:
                continue
            if event["event"] == "log":
                last_error = event["message"]
            if event["event"] == "dnssd":
                state["dnssd"] = event["state"]
                config.write_json_atomic(STATE_FILE, state)
            if event["event"] in ("job-done", "job-failed"):
                # The document and its (empty) .prn output: ippeveprinter only
                # removes them much later, and printed pages shouldn't linger.
                for f in SPOOL_DIR.glob(f"{event['job']}-*"):
                    f.unlink(missing_ok=True)
                done = [j for j in last_jobs(5) if str(j.get("job")) == str(event["job"])]
                if done:
                    event["result"] = done[-1]
                clean_spool()
            _emit(event)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
        STATE_FILE.unlink(missing_ok=True)
        lock.close()
    stopped = {"event": "stopped", "code": code}
    if code not in (0, -signal.SIGTERM) and last_error:
        stopped["error"] = last_error
    _emit(stopped)
    return {"ok": code in (0, -signal.SIGTERM)}


def serve_command(settings: dict) -> list[str]:
    """argv the daemon should spawn and keep alive; [] when the printer is
    switched off (printerEnabled) or ippeveprinter is missing."""
    if not settings.get("printerEnabled", True) or not ippeveprinter():
        return []
    return launcher() + ["printer", "serve"]


# ------------------------------------------------------------------ status


def read_state() -> dict:
    try:
        value = json.loads(STATE_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _avahi_unescape(s: str) -> str:
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), s).replace("\\.", ".").replace("\\\\", "\\")


def adverts(name: str = NAME) -> list[dict]:
    """Resolved DNS-SD _ipp._tcp records with our service name."""
    exe = shutil.which("avahi-browse")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "-rpt", "_ipp._tcp"], capture_output=True, text=True, timeout=8).stdout
    except subprocess.TimeoutExpired:
        return []
    found = []
    for line in out.splitlines():
        f = line.split(";")
        if len(f) < 9 or f[0] != "=":
            continue
        service = _avahi_unescape(f[3])
        if service == name or re.fullmatch(re.escape(name) + r" #\d+", service):
            found.append({"service": service, "interface": f[1], "protocol": f[2], "host": f[6],
                          "address": f[7], "port": int(f[8]) if f[8].isdigit() else None})
    return found


def queue_info() -> dict | None:
    exe = shutil.which("lpstat")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "-v", QUEUE], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return None
    # A printer CUPS only knows from DNS-SD exits 0 with no "device for" line.
    m = re.search(rf"^device for {re.escape(QUEUE)}:\s*(\S+)", proc.stdout, re.M)
    if proc.returncode != 0 or not m:
        return None
    return {"name": QUEUE, "uri": m.group(1)}


_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):
    """The daemon asks for status every few seconds; avahi-browse and lpstat
    take about a second each, so their answers are reused for a while."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.monotonic(), value)
    return value


def status(fresh: bool = False) -> dict:
    """Is the printer running, on which port, and do print dialogs see it?
    Always has "running", "port" (int or None) and "advertised"; "error"
    says what's wrong when something is."""
    state = read_state()
    alive = bool(state) and _alive(state.get("pid")) and _alive(state.get("ippPid"))
    port = int(state["port"]) if alive and state.get("port") else None
    running = bool(port) and _listening(port)
    name = state.get("name", NAME)
    result: dict[str, Any] = {
        "ok": True, "running": running, "port": port, "pid": state.get("pid") if alive else None,
        "name": name, "url": state.get("url") if running else None,
        "installed": bool(ippeveprinter()), "enabled": bool(config.load_settings().get("printerEnabled", True)),
        "dryRun": state.get("dryRun") if alive else None, "folderOverride": state.get("folder") if alive else None,
        "lastJob": (last_jobs(1) or [None])[0], "advertised": False, "interfaces": [],
    }
    ttl = 0 if fresh else 60
    if running:
        dnssd = state.get("dnssd")
        if dnssd == "registered" and not fresh:
            result["advertised"] = True  # avahi confirmed the registration (serve logs it)
        else:
            ours = [a for a in _cached(f"adverts:{name}", ttl, lambda: adverts(name)) if a["port"] == port]
            result["advertised"] = bool(ours)
            result["interfaces"] = sorted({a["interface"] for a in ours})
        if dnssd in ("collision", "failed") and not result["advertised"]:
            result["error"] = "print dialogs can't see the printer (DNS-SD registration failed; is avahi-daemon running?)"
    q = _cached("queue", 0 if fresh else 120, queue_info)
    result["queue"] = q
    if q and port:
        result["queueMatches"] = q["uri"].startswith(f"ipp://localhost:{port}/")
    if not result["installed"]:
        result["error"] = "ippeveprinter isn't installed (it comes with CUPS)"
    elif alive and not running:
        result["error"] = "the printer isn't answering on its port"
    elif state and not alive:
        result["error"] = "the printer stopped unexpectedly"
    return result


def cmd_status(args: argparse.Namespace) -> dict:
    return status(fresh=True)


# ------------------------------------------------------- persistent queue


def install_command(port: int) -> list[str]:
    return ["lpadmin", "-p", QUEUE, "-E", "-v", f"ipp://localhost:{port}/ipp/print", "-m", "everywhere",
            "-D", "reMarkable", "-L", "Omarchy", "-o", "printer-is-shared=false"]


def _lpadmin(cmd: list[str]) -> dict:
    if not shutil.which("lpadmin"):
        return {"ok": False, "error": "lpadmin isn't installed (it comes with CUPS)", "code": "printer"}
    try:
        # No stdin: lpadmin would otherwise sit waiting for a password prompt.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "CUPS didn't answer in time", "command": shlex.join(cmd), "code": "printer"}
    if proc.returncode == 0:
        return {"ok": True, "command": shlex.join(cmd)}
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    message = detail[-1] if detail else f"lpadmin exited {proc.returncode}"
    if re.search(r"forbidden|unauthori[sz]ed|password|not allowed|permission", message, re.I):
        message = "CUPS wants an administrator to change printers; run the command below in a terminal"
    return {"ok": False, "error": message, "command": shlex.join(cmd), "code": "printer"}


def cmd_install(args: argparse.Namespace) -> dict:
    """A permanent CUPS queue, for programs that only list installed
    printers. Print dialogs already see the advertised printer without it."""
    st = status()
    port = args.port or st.get("port") or DEFAULT_PORT
    if not st["running"] and not args.port:
        return {"ok": False, "code": "printer",
                "error": "start the reMarkable printer first (CUPS asks it what it supports)"}
    result = _lpadmin(install_command(int(port)))
    if result["ok"]:
        result.update(queue=QUEUE, port=port)
    return result


def cmd_remove(args: argparse.Namespace) -> dict:
    if queue_info() is None:
        return {"ok": True, "removed": False, "queue": QUEUE}
    result = _lpadmin(["lpadmin", "-x", QUEUE])
    if result["ok"]:
        result.update(removed=True, queue=QUEUE)
    return result


def register(sub) -> None:
    p = sub.add_parser("printer", help='a "reMarkable" printer for every print dialog')
    psub = p.add_subparsers(dest="action", required=True)

    x = psub.add_parser("serve", help="run the printer in the foreground (JSON lines)")
    x.add_argument("--port", type=int, help=f"default {DEFAULT_PORT}, or the next free one")
    x.add_argument("--name", default=NAME, help="name print dialogs show")
    x.add_argument("--folder", help=f"upload into this cloud folder instead of printFolder (sets {FOLDER_ENV})")
    x.add_argument("--dry-run", metavar="DIR", help=f"copy jobs into DIR instead of uploading (sets {DRYRUN_ENV})")
    x.add_argument("--verbose", action="store_true", help="pass every ippeveprinter log line through")
    x.set_defaults(func=cmd_serve)

    x = psub.add_parser("job", help="handle one print job (ippeveprinter runs this)")
    x.add_argument("file")
    x.set_defaults(func=cmd_job)

    x = psub.add_parser("status", help="running? advertised? installed as a CUPS queue?")
    x.set_defaults(func=cmd_status)

    x = psub.add_parser("install", help="add a permanent CUPS queue (needs CUPS admin rights)")
    x.add_argument("--port", type=int, help="port the printer runs on (default: the running one)")
    x.set_defaults(func=cmd_install)

    x = psub.add_parser("remove", help="remove the permanent CUPS queue")
    x.set_defaults(func=cmd_remove)
