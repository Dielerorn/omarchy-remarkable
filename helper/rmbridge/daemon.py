"""`omarchy-remarkable daemon`: the long-running half, owned by the shell service.

Writes one JSON event per line on stdout and reads simple commands on stdin
("refresh", "usb-backup", "mirror", "index", "templates", "quit"). Heavy work
runs one job at a time on a worker thread so polling never stalls behind it.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import cloud, config

VERSION = "0.1.2"
TICK = 5


def _module(name: str):
    try:
        return importlib.import_module(f".{name}", __package__)
    except Exception:
        return None


class Daemon:
    def __init__(self) -> None:
        self.out_lock = threading.Lock()
        self.jobs: queue.Queue[tuple[str, Callable[[], Any]]] = queue.Queue()
        self.queued: set[str] = set()
        self.running_job: str | None = None
        self.settings = config.load_settings()
        self.settings_mtime = self._settings_mtime()
        self.snapshot: dict[str, str] = {}  # id -> modified, last seen
        self.have_snapshot = False
        self.last_poll = 0.0
        self.last_sync: float | None = None
        self.sync_error: str | None = None
        self.usb: dict = {"connected": False, "webInterface": False}
        self.device: dict | None = None
        self.ssh_ok = False
        self.last_ssh = 0.0
        self.last_info = 0.0
        self.printer_proc: subprocess.Popen | None = None
        self.printer_backoff = 0.0
        self.printer_state: dict = {"running": False}
        self.agenda_tried = 0.0
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.mods = {n: _module(n) for n in ("usb", "ssh", "agenda", "printer", "notes", "transcribe", "search", "mirror")}

    # ------------------------------------------------------------ plumbing

    def emit(self, event: str, **data: Any) -> None:
        line = json.dumps({"event": event, "time": time.time(), **data}, default=str)
        with self.out_lock:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                self.stop.set()

    def notify(self, title: str, body: str = "", urgency: str = "normal") -> None:
        from .cli import notify
        notify(title, body, urgency=urgency)

    def submit(self, name: str, fn: Callable[[], Any]) -> None:
        if name in self.queued or name == self.running_job:
            return
        self.queued.add(name)
        self.jobs.put((name, fn))

    def worker(self) -> None:
        while not self.stop.is_set():
            try:
                name, fn = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            self.queued.discard(name)
            self.running_job = name
            self.emit("job", name=name, state="start")
            try:
                result = fn()
                self.emit("job", name=name, state="done", result=result)
            except cloud.NotPaired as e:
                self.emit("job", name=name, state="error", error=str(e), code="not-paired")
            except Exception as e:
                self.emit("job", name=name, state="error", error=f"{e}", trace=traceback.format_exc(limit=3))
            finally:
                self.running_job = None
                self.emit_status()

    def stdin_reader(self) -> None:
        for raw in sys.stdin:
            cmd = raw.strip()
            if not cmd:
                continue
            if cmd == "quit":
                self.stop.set()
                self.wake.set()
                return
            if cmd == "refresh":
                self.last_poll = 0
                self.last_ssh = 0
                self.last_info = 0
            elif cmd == "usb-backup":
                self.queue_usb_backup()
            elif cmd == "mirror":
                self.queue_mirror()
            elif cmd == "index":
                self.queue_index()
            elif cmd == "templates":
                self.queue_templates()
            elif cmd == "status":
                self.emit_status()
            self.wake.set()
        # stdin closed: the shell went away.
        self.stop.set()
        self.wake.set()

    def _settings_mtime(self) -> float:
        try:
            return config.SETTINGS_FILE.stat().st_mtime
        except OSError:
            return 0.0

    def emit_status(self) -> None:
        self.emit(
            "status",
            version=VERSION,
            paired=cloud.is_paired(),
            lastSync=self.last_sync,
            syncError=self.sync_error,
            documents=sum(1 for _ in self.snapshot),
            usb=self.usb,
            ssh=self.ssh_ok,
            device=self.device,
            printer=self.printer_state,
            job=self.running_job,
            queued=sorted(self.queued),
            settings=self.settings,
        )

    # -------------------------------------------------------------- cloud

    def poll_library(self) -> None:
        if not cloud.is_paired():
            if self.sync_error != "not-paired":
                self.sync_error = "not-paired"
                self.emit_status()
            return
        from .library import save_library_cache, cached_entries
        try:
            lib = cloud.library()
        except cloud.NotPaired:
            self.sync_error = "not-paired"
            self.emit_status()
            return
        except Exception as e:
            self.sync_error = str(e)
            self.emit_status()
            return
        save_library_cache(lib)
        self.sync_error = None
        self.last_sync = time.time()
        docs = {e.id: e for e in lib.documents()}
        changes = []
        if self.have_snapshot:
            mine = cloud.self_writes()
            for doc_id, e in docs.items():
                before = self.snapshot.get(doc_id)
                if before == e.modified:
                    continue
                kind = "new" if before is None else "changed"
                changes.append({"id": doc_id, "name": e.name, "path": e.path, "kind": kind, "self": doc_id in mine})
            for doc_id in self.snapshot:
                if doc_id not in docs:
                    changes.append({"id": doc_id, "kind": "removed"})
        self.snapshot = {i: e.modified for i, e in docs.items()}
        first = not self.have_snapshot
        self.have_snapshot = True
        self.emit("library", entries=cached_entries(), changes=changes, initial=first)

        foreign = [c for c in changes if c["kind"] != "removed" and not c.get("self")]
        if foreign and self.settings.get("notifyNewNotes"):
            if len(foreign) == 1:
                c = foreign[0]
                self.notify("New on your reMarkable" if c["kind"] == "new" else "Updated on your reMarkable", c["name"])
            else:
                names = ", ".join(c["name"] for c in foreign[:3])
                self.notify(f"{len(foreign)} documents changed on your reMarkable", names + ("…" if len(foreign) > 3 else ""))
        if changes or first:
            self.after_library_change(lib, first)

    def after_library_change(self, lib: cloud.Library, first: bool) -> None:
        if self.settings.get("mirrorEnabled"):
            self.queue_mirror(lib)
        m = self.mods
        if m["transcribe"] and hasattr(m["transcribe"], "auto") and self.settings.get("autoTranscribeFolders") \
                and self.settings.get("transcribeEngine") != "off":
            self.submit("transcribe", lambda: self._with_state(lambda st: m["transcribe"].auto(lib, self.settings, st)))
        if m["notes"] and hasattr(m["notes"], "sync_changed"):
            self.submit("notes", lambda: [str(p) for p in self._with_state(lambda st: m["notes"].sync_changed(lib, st))])
        self.queue_index(lib)

    def _with_state(self, fn: Callable[[dict], Any]) -> Any:
        state = config.load_state()
        result = fn(state)
        fresh = config.load_state()
        fresh.update(state)
        config.save_state(fresh)
        return result

    def queue_mirror(self, lib: cloud.Library | None = None) -> None:
        mirror = self.mods["mirror"]
        if mirror:
            self.submit("mirror", lambda: _summary(mirror.run_mirror(lib)))

    def queue_index(self, lib: cloud.Library | None = None) -> None:
        search = self.mods["search"]
        if search and hasattr(search, "update"):
            self.submit("index", lambda: search.update(lib))

    # ---------------------------------------------------------- send folder

    def check_send_folder(self) -> None:
        mirror = self.mods["mirror"]
        if not mirror or not self.settings.get("sendFolderEnabled"):
            return
        if mirror.pending_sends(self.settings):
            def job():
                result = mirror.scan_send_folder(self.settings)
                if result["sent"]:
                    names = ", ".join(r["name"] for r in result["sent"][:3])
                    self.notify("Sent to reMarkable", names)
                for err in result["errors"]:
                    self.notify("Couldn't send to reMarkable", f"{Path(err['file']).name}: {err['error']}", "critical")
                self.last_poll = 0
                return result
            self.submit("sendfolder", job)

    # -------------------------------------------------------------- devices

    def check_usb(self) -> None:
        usb = self.mods["usb"]
        if not usb or not hasattr(usb, "detect"):
            return
        try:
            now = usb.detect(timeout=1.0)
        except Exception:
            return
        was = self.usb.get("connected")
        if now != self.usb:
            self.usb = now
            self.emit("usb", **now)
            self.emit_status()
        if now.get("connected") and not was:
            self.last_ssh = 0  # a USB link usually means SSH just became possible
            if now.get("webInterface") and self.settings.get("usbAutoBackup"):
                self.queue_usb_backup()

    def queue_usb_backup(self) -> None:
        usb = self.mods["usb"]
        if not usb or not hasattr(usb, "backup"):
            return
        dest = config.setting_path(self.settings, "backupDir") / "usb"

        def job():
            result = usb.backup(dest)
            self.notify("reMarkable backup finished", f"Saved to {dest}")
            return _summary(result)
        self.submit("usb-backup", job)

    def check_ssh(self) -> None:
        ssh = self.mods["ssh"]
        if not ssh or not hasattr(ssh, "reachable"):
            return
        now = time.time()
        if now - self.last_ssh < (20 if self.ssh_ok else 45):
            return
        self.last_ssh = now
        try:
            ok = bool(ssh.reachable(timeout=1.5))
        except Exception:
            ok = False
        if ok != self.ssh_ok:
            self.ssh_ok = ok
            if not ok:
                self.device = None
            self.emit_status()
            if ok and not any(config.CACHE_DIR.joinpath("templates").glob("*.svg")):
                self.queue_templates()
        if ok and now - self.last_info > 60:
            self.last_info = now
            try:
                info = ssh.info()
            except Exception as e:
                info = {"error": str(e)}
            low_before = (self.device or {}).get("battery", 100)
            self.device = info
            self.emit("device", **info)
            battery = info.get("battery")
            if isinstance(battery, (int, float)) and battery <= 15 < (low_before if isinstance(low_before, (int, float)) else 100) \
                    and not info.get("charging"):
                self.notify("reMarkable battery low", f"{int(battery)}% left", "critical")

    def queue_templates(self) -> None:
        ssh = self.mods["ssh"]
        fn = getattr(ssh, "fetch_templates", None) if ssh else None
        if fn:
            self.submit("templates", lambda: _summary(fn()))

    # -------------------------------------------------------------- printer

    def check_printer(self) -> None:
        printer = self.mods["printer"]
        if not printer or not hasattr(printer, "serve_command"):
            return
        want = bool(self.settings.get("printerEnabled")) and cloud.is_paired()
        running = self.printer_proc is not None and self.printer_proc.poll() is None
        if want and not running:
            if time.time() < self.printer_backoff:
                return
            try:
                argv = printer.serve_command(self.settings)
                if not argv:  # disabled, or ippeveprinter isn't installed
                    self.printer_backoff = time.time() + 300
                    status = printer.status() if hasattr(printer, "status") else {}
                    state = {"running": False, **({"error": status["error"]} if status.get("error") else {})}
                    if state != self.printer_state:
                        self.printer_state = state
                        self.emit_status()
                    return
                self.printer_proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL, preexec_fn=_die_with_parent)
                self.printer_backoff = time.time() + 30
            except Exception as e:
                self.printer_state = {"running": False, "error": str(e)}
                self.printer_backoff = time.time() + 300
                self.emit_status()
                return
        elif not want and running:
            self.stop_printer()
        running = self.printer_proc is not None and self.printer_proc.poll() is None
        state = {"running": running}
        if running and hasattr(printer, "status"):
            try:
                state.update(printer.status())
            except Exception:
                pass
        if state != self.printer_state:
            self.printer_state = state
            self.emit_status()

    def _on_signal(self, signum, frame) -> None:
        self.stop.set()
        self.wake.set()

    def stop_printer(self) -> None:
        if self.printer_proc and self.printer_proc.poll() is None:
            self.printer_proc.terminate()
            try:
                self.printer_proc.wait(5)
            except subprocess.TimeoutExpired:
                self.printer_proc.kill()
        self.printer_proc = None

    # ------------------------------------------------------------ schedules

    def check_schedules(self) -> None:
        agenda = self.mods["agenda"]
        if agenda and hasattr(agenda, "maybe_run") and self.settings.get("agendaEnabled") and cloud.is_paired():
            now = datetime.now()
            # Only once it's time, and at most once a minute (maybe_run has its
            # own 15-minute retry after a failure), so idle ticks stay quiet.
            if now.strftime("%H:%M") < _hhmm(self.settings.get("agendaTime", "06:00")) or time.time() - self.agenda_tried < 60:
                return
            state = config.load_state()
            today = now.strftime("%Y-%m-%d")
            if state.get("agendaLastDate") != today and "agenda" not in self.queued and self.running_job != "agenda":
                self.agenda_tried = time.time()
                def job():
                    result = self._with_state(lambda st: agenda.maybe_run(self.settings, st, datetime.now()))
                    if result:
                        self.notify("Today's agenda is on your reMarkable", result.get("name", ""))
                        self.last_poll = 0
                    return result
                self.submit("agenda", job)

    # ----------------------------------------------------------------- loop

    def run(self) -> None:
        from .library import cached_entries
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._on_signal)
        _stop_orphan_printers()
        self.emit("hello", version=VERSION, paired=cloud.is_paired(), pid=_pid())
        cached = cached_entries()
        if cached:
            self.emit("library", entries=cached, changes=[], initial=True, cached=True)
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.stdin_reader, daemon=True).start()
        self.emit_status()
        while not self.stop.is_set():
            mtime = self._settings_mtime()
            if mtime != self.settings_mtime:
                self.settings_mtime = mtime
                self.settings = config.load_settings()
                self.emit_status()
            now = time.time()
            try:
                if now - self.last_poll >= max(30, int(self.settings.get("pollSeconds", 90))):
                    self.last_poll = now
                    self.poll_library()
                self.check_usb()
                self.check_ssh()
                self.check_send_folder()
                self.check_printer()
                self.check_schedules()
            except Exception as e:
                self.emit("error", error=str(e), trace=traceback.format_exc(limit=3))
            self.wake.wait(TICK)
            self.wake.clear()
        self.stop_printer()


def _die_with_parent() -> None:
    """In the child: get SIGTERM if the daemon dies, however it dies."""
    import ctypes
    PR_SET_PDEATHSIG = 1
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except OSError:
        pass


def _stop_orphan_printers() -> None:
    """Only one daemon runs (lock), so any printer serve already running was
    left behind by a daemon that was killed; stop it so ours can take over."""
    try:
        out = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "-x",
                              r".*python[0-9.]* -m rmbridge printer serve.*"],
                             capture_output=True, text=True).stdout
    except OSError:
        return
    for pid in out.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    if out.strip():
        time.sleep(1.5)


def _hhmm(value: Any) -> str:
    """"6:5" / "06:05" -> "06:05", so times compare as strings."""
    try:
        h, m = str(value).strip().split(":")[:2]
        return f"{int(h):02d}:{int(m):02d}"
    except (ValueError, TypeError):
        return "06:00"


def _summary(result: Any) -> Any:
    """Job results go to the UI; keep long lists short."""
    if isinstance(result, dict):
        out = {}
        for k, v in result.items():
            out[k] = len(v) if isinstance(v, list) and len(v) > 20 else v
        return out
    return result


def _pid() -> int:
    import os
    return os.getpid()


def cmd_daemon(args: argparse.Namespace) -> dict | None:
    args.streaming = True
    config.ensure_dirs()
    lock = open(config.RUNTIME_DIR / "daemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"event": "duplicate", "error": "another daemon is already running"}), flush=True)
        # Wait instead of exiting so the shell doesn't respawn us in a loop;
        # take over as soon as the other one goes away.
        fcntl.flock(lock, fcntl.LOCK_EX)
    Daemon().run()
    return None


def register(sub) -> None:
    p = sub.add_parser("daemon", help="run the background service (the shell starts this)")
    p.set_defaults(func=cmd_daemon)
