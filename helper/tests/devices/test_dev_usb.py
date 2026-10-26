"""usb.py against a fake USB web interface."""

from __future__ import annotations

import json
import socket
import time
import zipfile

import pytest

from fake_usb_server import FakeUsbServer
from rmbridge import cli, usb


@pytest.fixture
def server(monkeypatch):
    with FakeUsbServer() as srv:
        monkeypatch.setenv("OMARCHY_REMARKABLE_USB_HOST", f"127.0.0.1:{srv.port}")
        yield srv


def run_cli(argv, capsys) -> dict:
    code = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return {"code": code, **json.loads(out[-1])}


# ------------------------------------------------------------ detect


def test_detect_finds_web_interface(server):
    server.tablet.add("Notes")
    state = usb.detect(timeout=1.0)
    assert state == {"connected": True, "webInterface": True, "host": "127.0.0.1"}


def test_detect_port_open_but_not_the_web_interface(monkeypatch):
    # Something listening that never answers HTTP: connected, web interface off, and within the timeout.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    monkeypatch.setenv("OMARCHY_REMARKABLE_USB_HOST", f"127.0.0.1:{sock.getsockname()[1]}")
    started = time.monotonic()
    state = usb.detect(timeout=0.5)
    elapsed = time.monotonic() - started
    sock.close()
    assert state["connected"] is True and state["webInterface"] is False
    assert elapsed < 0.8


def test_detect_nothing_there_is_fast(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens: refused
    monkeypatch.setenv("OMARCHY_REMARKABLE_USB_HOST", f"127.0.0.1:{port}")
    started = time.monotonic()
    assert usb.detect(timeout=1.0) == {"connected": False, "webInterface": False, "host": "127.0.0.1"}
    assert time.monotonic() - started < 0.5


def test_detect_blackhole_respects_timeout(monkeypatch):
    # 10.255.255.1 is unroutable on most networks; whatever happens, the timeout holds.
    monkeypatch.setattr(usb, "HOST", "10.255.255.1")
    monkeypatch.setattr(usb, "usb_link_up", lambda host: True)  # pretend the cable is in
    started = time.monotonic()
    state = usb.detect(timeout=0.4)
    assert time.monotonic() - started < 0.7
    assert state["webInterface"] is False


# ------------------------------------------------------------ listing


def test_list_normalizes_both_spellings_and_utf8(server):
    server.tablet.add("Säntis – Tagebuch", file_type="pdf", modified="2026-03-09T20:06:11.193Z")
    server.tablet.add("Work", folder=True)
    entries = usb.list_folder("")
    names = {e["name"]: e for e in entries}
    assert "Säntis – Tagebuch" in names
    doc = names["Säntis – Tagebuch"]
    assert doc["folder"] is False and doc["fileType"] == "pdf" and doc["modified"] == "2026-03-09T20:06:11.193Z"
    assert names["Work"]["folder"] is True and names["Work"]["fileType"] is None


def test_list_legacy_firmware_fields(server):
    server.tablet.legacy = True
    server.tablet.add("Old")
    (e,) = usb.list_folder("")
    assert e["name"] == "Old" and e["sizeInBytes"] == 12345 and e["pageCount"] == 3


def test_list_unknown_folder_is_an_error_not_the_root(server):
    server.tablet.add("At top")
    doc = server.tablet.add("A document")
    with pytest.raises(usb.UsbError, match="no folder"):
        usb.list_folder(doc)


def test_walk_recurses_with_paths(server):
    t = server.tablet
    work = t.add("Work", folder=True)
    proj = t.add("Projects", parent=work, folder=True)
    t.add("Plan", parent=proj)
    t.add("Top")
    entries = usb.walk()
    paths = sorted(e["path"] for e in entries)
    assert paths == ["/Top", "/Work", "/Work/Projects", "/Work/Projects/Plan"]


def test_cli_list_recursive_under_folder(server, capsys):
    t = server.tablet
    work = t.add("Work", folder=True)
    t.add("Inside", parent=work)
    t.add("Outside")
    result = run_cli(["usb", "list", "--recursive", "--folder", work], capsys)
    assert result["ok"] and [e["path"] for e in result["entries"]] == ["/Work/Inside"]


# ------------------------------------------------------------ download / upload


def test_download_uses_device_filename(server, tmp_path):
    doc = server.tablet.add("Résumé notes", file_type="notebook")
    pdf = usb.download(doc, "pdf", tmp_path / "out")
    assert pdf.name == "Résumé notes.pdf" and pdf.read_bytes().startswith(b"%PDF")
    rmdoc = usb.download(doc, "rmdoc", tmp_path / "out")
    assert zipfile.ZipFile(rmdoc).namelist() == [f"{doc}.metadata", f"{doc}.content"]


def test_download_errors_are_plain_english(server, tmp_path):
    folder = server.tablet.add("F", folder=True)
    with pytest.raises(usb.UsbError, match="no document"):
        usb.download("00000000-0000-0000-0000-000000000000", "pdf", tmp_path)
    with pytest.raises(usb.UsbError, match="folder"):
        usb.download(folder, "pdf", tmp_path)
    assert not list(tmp_path.glob(".download-*"))


def test_upload_lists_target_folder_last(server, tmp_path):
    t = server.tablet
    folder = t.add("Inbox", folder=True)
    src = tmp_path / "paper.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    result = usb.upload(src, folder)
    assert t.uploads[-1]["folder"] == folder
    assert result["landedIn"] == folder and result["name"] == "paper.pdf" and result["id"]
    assert "warning" not in result
    assert 'Content-Type: application/pdf' in t.uploads[-1]["head"]


def test_upload_converts_images_to_pdf(server, tmp_path):
    import pymupdf
    img = tmp_path / "shot.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
    pix.clear_with(200)
    pix.save(img)
    result = usb.upload(img, "", name="My Screenshot")
    up = server.tablet.uploads[-1]
    assert up["filename"] == "My Screenshot.pdf" and up["data"].startswith(b"%PDF")
    assert result["landedIn"] == ""


def test_upload_reports_when_it_lands_at_top_level(server, tmp_path, monkeypatch):
    # Firmware that ignores the last listing: the result says so.
    t = server.tablet
    folder = t.add("Inbox", folder=True)
    real_list = usb.list_folder

    def list_but_forget(f=""):
        out = real_list(f)
        t.last_listed = ""
        return out

    monkeypatch.setattr(usb, "list_folder", list_but_forget)
    src = tmp_path / "a.epub"
    src.write_bytes(b"PK epub")
    result = usb.upload(src, folder)
    assert result["landedIn"] == "" and "top level" in result["warning"]


# ------------------------------------------------------------ backup


def test_backup_is_incremental(server, tmp_path):
    t = server.tablet
    work = t.add("Work", folder=True)
    a = t.add("Plan", parent=work, modified="2026-10-01T00:00:00Z")
    b = t.add("Diary", modified="2026-10-01T00:00:00Z")
    t.add("Plan", parent=work, modified="2026-10-01T00:00:00Z", doc_id="dup00000-0000")  # same name
    dest = tmp_path / "bk"
    seen = []
    first = usb.backup(dest, on_progress=lambda *a: seen.append(a))
    assert first["downloaded"] == 3 and first["skipped"] == 0 and first["failed"] == 0
    assert (dest / "Work/Plan.rmdoc").exists() and (dest / "Work/Plan.pdf").exists()
    assert (dest / "Work/Plan (dup00000).rmdoc").exists()
    assert (dest / "Diary.pdf").exists() and seen
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["documents"][a]["modified"] == "2026-10-01T00:00:00Z"

    t.downloads.clear()
    second = usb.backup(dest)
    assert second["downloaded"] == 0 and second["skipped"] == 3 and t.downloads == []

    t.entries[b]["modified"] = "2026-10-02T00:00:00Z"
    t.entries[a]["name"] = "Plan v2"
    third = usb.backup(dest)
    assert third["downloaded"] == 1 and third["moved"] == 1
    assert sorted(t.downloads) == sorted([f"{b}/rmdoc", f"{b}/pdf"])
    assert (dest / "Work/Plan v2.rmdoc").exists() and not (dest / "Work/Plan.rmdoc").exists()


def test_cli_status_and_backup(server, capsys, isolated):
    server.tablet.add("Only")
    status = run_cli(["usb", "status"], capsys)
    assert status["ok"] and status["webInterface"] and status["topLevel"] == 1
    result = run_cli(["usb", "backup"], capsys)
    assert result["ok"] and result["downloaded"] == 1
    assert result["path"] == str(isolated / "backups" / "usb")


def test_cli_reports_unreachable(monkeypatch, capsys):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("OMARCHY_REMARKABLE_USB_HOST", f"127.0.0.1:{port}")
    result = run_cli(["usb", "list"], capsys)
    assert result["ok"] is False and result["code"] == "usb" and "USB web interface" in result["error"]


def test_unplugged_answers_without_waiting(monkeypatch):
    monkeypatch.setattr(usb, "usb_link_up", lambda host: False)
    started = time.monotonic()
    assert usb.detect(timeout=5.0)["connected"] is False
    assert time.monotonic() - started < 0.1


def test_usb_link_check_needs_a_10_11_99_address():
    from rmbridge.tablet_remote import usb_link_up
    assert usb_link_up("127.0.0.1") is False  # loopback source address, not the tablet's subnet
