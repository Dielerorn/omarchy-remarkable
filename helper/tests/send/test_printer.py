"""printer.py: job naming, the job handler, log parsing, and (when CUPS's
tools are installed) a real ippeveprinter round trip in dry-run mode."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pymupdf
import pytest

from rmbridge import cloud, printer

HELPER = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 26, 7, 5)


@pytest.mark.parametrize("job,file,expected", [
    ("Quarterly Report.docx", "", "Quarterly Report"),
    ("Microsoft Word - Letter to Bank.docx", "", "Letter to Bank"),
    ("Things we learned about LLMs in 2024", "", "Things we learned about LLMs in 2024"),
    ("Untitled", "/spool/3-untitled.pdf", "Printed 2026-10-26 07.05"),
    ("", "/spool/4-boarding_pass.pdf", "boarding_pass"),
    ("  lots   of\tspace.pdf ", "", "lots of space"),
])
def test_job_title(job, file, expected):
    assert printer.job_title(job, file, NOW) == expected


def test_parse_log():
    p = printer.parse_log
    assert p('Created job file "/s/5-x.pdf", format "application/pdf".') == \
        {"event": "job-received", "file": "/s/5-x.pdf", "format": "application/pdf"}
    assert p("Ignored Avahi state 2.") == {"event": "dnssd", "state": "registered"}
    assert p("Ignored Avahi state 3.") == {"event": "dnssd", "state": "collision"}
    assert p("Accepted connection from localhost") is None
    assert p("[Job 3] Processing.") is None
    assert p("Unable to bind socket: Address in use")["event"] == "log"
    assert printer.job_id_of("/s/12-secret_title.pdf") == 12 and printer.job_id_of("/s/x.pdf") is None


def test_ipp_command_keeps_job_titles_off_command_lines(monkeypatch):
    monkeypatch.setattr(printer, "ippeveprinter", lambda: "/usr/bin/ippeveprinter")
    cmd = printer.ipp_command(8631)
    assert "-c" not in cmd and "-k" in cmd


def test_ipp_request_and_response():
    req = printer.ipp_get_job_request(8631, 7, request_id=3)
    assert req[:8] == b"\x02\x00\x00\x09\x00\x00\x00\x03" and req.endswith(b"\x03")
    assert b"ipp://localhost:8631/ipp/print" in req and b"job-name" in req and b"job-state" in req

    def attr(tag, name, value):
        return printer._ipp_attr(tag, name, value)
    title = "Secret Title Ü".encode()
    resp = (b"\x02\x00\x00\x00\x00\x00\x00\x03\x01" + attr(0x47, "attributes-charset", b"utf-8")
            + b"\x02" + attr(0x42, "job-name", title) + attr(0x23, "job-state", (9).to_bytes(4, "big"))
            + attr(0x49, "document-format", b"application/pdf")
            + attr(0x36, "job-originating-user-name", b"\x00\x02en\x00\x05alice") + b"\x03")
    status, attrs = printer.ipp_parse_response(resp)
    assert status == 0
    assert attrs["job-name"] == "Secret Title Ü" and attrs["job-state"] == 9
    assert attrs["document-format"] == "application/pdf" and attrs["job-originating-user-name"] == "alice"
    env = printer.job_env(attrs, 7, "", {"X": "1"})
    assert env["IPP_JOB_NAME"] == "Secret Title Ü" and env["IPP_JOB_ID"] == "7" and env["X"] == "1"


def make_pdf(path: Path, text: str = "hello") -> Path:
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), text)
    d.save(path)
    return path


def job_env(**kw) -> dict:
    env = {"CONTENT_TYPE": "application/pdf", "IPP_JOB_NAME": "Trip Plan.pdf", "IPP_JOB_ID": "7",
           "IPP_JOB_ORIGINATING_USER_NAME": "alice"}
    env.update(kw)
    return env


def test_handle_job_dry_run(tmp_path):
    src = make_pdf(tmp_path / "7-trip_plan_pdf.pdf")
    out = tmp_path / "dry"
    result = printer.handle_job(src, job_env(**{printer.DRYRUN_ENV: str(out)}))
    assert result["dryRun"] and result["name"] == "Trip Plan" and result["folder"] == "Printed"
    assert (out / "Trip Plan.pdf").read_bytes() == src.read_bytes()


def test_handle_job_uploads(tmp_path, fake_cloud):
    src = tmp_path / "spoolfile"  # no extension: the type comes from CONTENT_TYPE / the bytes
    make_pdf(src)
    fake_cloud.add("Printed/Trip Plan")
    result = printer.handle_job(src, job_env())
    assert result["name"] == "Trip Plan (2)" and result["folder"] == "Printed" and not result["dryRun"]
    up = fake_cloud.uploads[-1]
    assert up["src"].endswith(".pdf") and up["folder"] == "Printed" and up["bytes"] == src.read_bytes()
    result = printer.handle_job(src, job_env(CONTENT_TYPE="application/octet-stream",
                                             **{printer.FOLDER_ENV: "Omarchy Plugin Test"}))
    assert result["folder"] == "Omarchy Plugin Test" and fake_cloud.uploads[-1]["src"].endswith(".pdf")


def test_handle_job_rejects_unknown_formats(tmp_path):
    src = tmp_path / "x.ps"
    src.write_bytes(b"%!PS-Adobe-3.0\n")
    with pytest.raises(printer.PrinterError, match="print as PDF"):
        printer.handle_job(src, job_env(CONTENT_TYPE="application/postscript"))


def test_process_job_failure(tmp_path, monkeypatch, notifications, fake_cloud):
    src = make_pdf(tmp_path / "1-x.pdf")

    def expired(*a, **k):
        raise cloud.NotPaired("the cloud login has expired; connect again")
    monkeypatch.setattr("rmbridge.send.send_file", expired)
    result = printer.process_job(src, job_env())
    assert result["ok"] is False and "expired" in result["error"]
    assert notifications[0][0] == "Couldn't print to reMarkable"
    assert printer.last_jobs(1)[0]["ok"] is False


def test_process_job_success(tmp_path, notifications, fake_cloud):
    src = make_pdf(tmp_path / "1-x.pdf")
    result = printer.process_job(src, job_env())
    assert result["ok"] and result["name"] == "Trip Plan"
    assert notifications == [("Printed to reMarkable", "Trip Plan", {"icon": "printer"})]
    assert printer.last_jobs(1)[0]["name"] == "Trip Plan"


def test_serve_command(monkeypatch):
    monkeypatch.setattr(printer, "ippeveprinter", lambda: "/usr/bin/ippeveprinter")
    assert printer.serve_command({"printerEnabled": False}) == []
    argv = printer.serve_command({"printerEnabled": True})
    assert argv[-2:] == ["printer", "serve"]
    monkeypatch.setattr(printer, "ippeveprinter", lambda: None)
    assert printer.serve_command({"printerEnabled": True}) == []


def test_status_when_stopped():
    st = printer.status()
    assert st["running"] is False and st["port"] is None and st["advertised"] is False
    printer.STATE_FILE.write_text(json.dumps({"pid": 999999999, "ippPid": 999999998, "port": 8631}))
    assert printer.status()["error"] == "the printer stopped unexpectedly"


def test_pick_port():
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        s.listen()
        busy = s.getsockname()[1]
        with pytest.raises(printer.PrinterError, match="already in use"):
            printer.pick_port(busy)
    assert printer.DEFAULT_PORT <= printer.pick_port(None) < printer.DEFAULT_PORT + printer.PORT_TRIES


def test_install_needs_a_running_printer(monkeypatch):
    monkeypatch.setattr(printer, "_lpadmin", lambda cmd: pytest.fail("must not call lpadmin"))
    result = printer.cmd_install(argparse.Namespace(port=None))
    assert result["ok"] is False and "start the reMarkable printer first" in result["error"]
    assert printer.install_command(8631) == [
        "lpadmin", "-p", "reMarkable", "-E", "-v", "ipp://localhost:8631/ipp/print", "-m", "everywhere",
        "-D", "reMarkable", "-L", "Omarchy", "-o", "printer-is-shared=false"]


# ------------------------------------------------------------ integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not (shutil.which("ippeveprinter") and shutil.which("ipptool")),
                    reason="needs CUPS's ippeveprinter and ipptool")
def test_round_trip_dry_run(tmp_path):
    """serve -> ipptool Print-Job -> job attributes over IPP -> file in the
    dry-run dir, then SIGTERM cleans everything up."""
    env = dict(os.environ, PYTHONPATH=str(HELPER), XDG_RUNTIME_DIR=str(tmp_path / "run"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_DATA_HOME=str(tmp_path / "data"))
    (tmp_path / "run").mkdir(exist_ok=True)
    port, dry = _free_port(), tmp_path / "dry"
    proc = subprocess.Popen([sys.executable, "-m", "rmbridge", "printer", "serve", "--port", str(port),
                             "--name", f"reMarkable test {port}", "--dry-run", str(dry)],
                            stdout=subprocess.PIPE, text=True, env=env)
    try:
        started = json.loads(proc.stdout.readline())
        assert started["event"] == "started" and started["port"] == port
        for _ in range(50):  # wait for the listener
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        test = tmp_path / "named.test"
        test.write_text(Path("/usr/share/cups/ipptool/print-job.test").read_text().replace(
            "ATTR integer copies 1", 'ATTR integer copies 1\n\tATTR name job-name "Round Trip.pdf"'))
        pdf = make_pdf(tmp_path / "in.pdf", "round trip")
        sent = subprocess.run(["ipptool", "-t", "-f", str(pdf), f"ipp://localhost:{port}/ipp/print", str(test)],
                              capture_output=True, text=True, timeout=30)
        assert sent.returncode == 0, sent.stdout
        events = []
        deadline = time.time() + 30
        while time.time() < deadline:
            event = json.loads(proc.stdout.readline())
            events.append(event)
            if event["event"] in ("job-done", "job-failed"):
                break
        assert events[-1]["event"] == "job-done", events
        assert events[-1]["result"]["name"] == "Round Trip"
        assert (dry / "Round Trip.pdf").read_bytes() == pdf.read_bytes()
    finally:
        proc.send_signal(signal.SIGTERM)
        rest = proc.communicate(timeout=15)[0]
    assert proc.returncode == 0
    assert json.loads(rest.strip().splitlines()[-1])["event"] == "stopped"
    assert not (tmp_path / "run" / "omarchy-remarkable" / "printer.json").exists()
    assert not list((tmp_path / "cache" / "omarchy-remarkable" / "printer" / "spool").glob("*"))
