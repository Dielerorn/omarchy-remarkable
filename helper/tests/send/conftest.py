"""Fixtures for the web / agenda / printer tests: every path the helper
writes goes to a temp dir, notifications are recorded instead of shown, and
nothing touches the network or the cloud unless a test fakes it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from rmbridge import cli, cloud, config, library, printer  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point every config path at tmp_path."""
    dirs = {
        "CONFIG_DIR": tmp_path / "config", "CACHE_DIR": tmp_path / "cache", "DATA_DIR": tmp_path / "data",
        "RUNTIME_DIR": tmp_path / "run",
    }
    for name, path in dirs.items():
        monkeypatch.setattr(config, name, path)
    cache = dirs["CACHE_DIR"]
    for name, path in {
        "SETTINGS_FILE": dirs["CONFIG_DIR"] / "settings.json", "STATE_FILE": dirs["DATA_DIR"] / "state.json",
        "RMAPI_CONFIG": dirs["CONFIG_DIR"] / "rmapi.conf", "DOC_CACHE": cache / "docs",
        "RENDER_CACHE": cache / "renders", "THUMB_CACHE": cache / "thumbs", "TMP_DIR": cache / "tmp",
    }.items():
        monkeypatch.setattr(config, name, path)
    monkeypatch.setattr(library, "LIBRARY_FILE", dirs["DATA_DIR"] / "library.json")
    monkeypatch.setattr(library, "DOCMETA_FILE", dirs["DATA_DIR"] / "docmeta.json")
    pdir = cache / "printer"
    monkeypatch.setattr(printer, "PRINTER_DIR", pdir)
    monkeypatch.setattr(printer, "SPOOL_DIR", pdir / "spool")
    monkeypatch.setattr(printer, "JOBS_LOG", pdir / "jobs.jsonl")
    monkeypatch.setattr(printer, "STATE_FILE", dirs["RUNTIME_DIR"] / "printer.json")
    monkeypatch.setattr(printer, "LOCK_FILE", dirs["RUNTIME_DIR"] / "printer.lock")
    monkeypatch.setattr(printer, "WRAPPER", dirs["RUNTIME_DIR"] / "printer-job")
    printer._cache.clear()
    for var in (printer.FOLDER_ENV, printer.DRYRUN_ENV):
        monkeypatch.delenv(var, raising=False)
    config.ensure_dirs()
    return tmp_path


@pytest.fixture(autouse=True)
def notifications(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(cli, "notify", lambda title, body="", **kw: sent.append((title, body, kw)))
    return sent


class FakeCloud:
    """Stands in for cloud.library()/send.send_file: an in-memory library
    that grows as files are "uploaded"."""

    def __init__(self):
        self.entries = [cloud.Entry(id="", name="", is_folder=True, parent="", modified="")]
        self.uploads: list[dict] = []

    def add(self, path: str, folder: bool = False) -> cloud.Entry:
        parent = ""
        parts = [p for p in path.strip("/").split("/")]
        for i, part in enumerate(parts):
            sub = "/" + "/".join(parts[: i + 1])
            lib = cloud.Library(self.entries)
            found = lib.find_path(sub)
            if found is None:
                found = cloud.Entry(id=f"id{len(self.entries)}", name=part, is_folder=folder or i < len(parts) - 1,
                                    parent=parent, modified="2026-10-26T00:00:00Z")
                self.entries.append(found)
            parent = found.id
        return found

    def library(self) -> cloud.Library:
        return cloud.Library(list(self.entries))

    def send_file(self, src: Path, folder: str | None = None, name: str | None = None) -> dict:
        data = Path(src).read_bytes()
        e = self.add(f"{folder}/{name}" if folder else name)
        self.uploads.append({"src": str(src), "folder": folder, "name": name, "bytes": data})
        return {"file": str(src), "id": e.id, "name": name, "folder": folder}


@pytest.fixture
def fake_cloud(monkeypatch):
    from rmbridge import send
    fc = FakeCloud()
    monkeypatch.setattr(cloud, "library", fc.library)
    monkeypatch.setattr(library, "fetch_library", fc.library)
    monkeypatch.setattr(send, "send_file", fc.send_file)
    return fc
