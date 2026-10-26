"""Shared fixtures: every path the helper writes goes to a temp dir, and
library.open_document serves synthetic documents instead of the cloud."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rmbridge import cloud, config, library, transcribe  # noqa: E402
from rmbridge.doc import load  # noqa: E402


class FakeLibrary:
    """Documents by id -> (cloud.Entry, path to .rmdoc), served like the real library."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.docs: dict[str, tuple[cloud.Entry, Path]] = {}
        self.opened: list[str] = []

    def add(self, doc_id: str, path: str, rmdoc: Path, modified: str = "2026-01-01T00:00:00Z") -> cloud.Entry:
        name = path.rsplit("/", 1)[-1]
        e = cloud.Entry(id=doc_id, name=name, is_folder=False, parent="", modified=modified, path=path)
        self.docs[doc_id] = (e, rmdoc)
        return e

    def open_document(self, doc_id: str):
        self.opened.append(doc_id)
        if doc_id not in self.docs:
            raise cloud.CloudError(f"no document with id {doc_id}")
        e, path = self.docs[doc_id]
        return e, load(path)

    def library(self) -> cloud.Library:
        # Fresh Entry copies: Library() recomputes .path from parents, so keep ours.
        entries = []
        for e, _ in self.docs.values():
            entries.append(cloud.Entry(id=e.id, name=e.name, is_folder=False, parent="", modified=e.modified))
        lib = cloud.Library(entries)
        for e in lib.entries:
            e.path = self.docs[e.id][0].path
        return lib


@pytest.fixture(autouse=True)
def no_outside_world(monkeypatch):
    """Tests never reach Claude, the Anthropic API or the reMarkable cloud.

    A test that exercises an engine swaps in its own fake after this runs.
    """
    def blocked(*a, **k):
        raise AssertionError("a test tried to reach the network or Claude")

    monkeypatch.setattr(transcribe, "claude_executable", lambda: None)
    monkeypatch.setattr(transcribe, "run_engine", blocked)
    monkeypatch.setattr(transcribe.urllib.request, "urlopen", blocked)
    monkeypatch.setattr(cloud, "run", blocked)
    monkeypatch.setattr(library, "fetch_library", blocked)


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RENDER_CACHE", tmp_path / "cache" / "renders")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "cache" / "tmp")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "run")
    monkeypatch.setattr(config, "INDEX_DB", data / "index.db")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(transcribe, "TRANSCRIPTS", data / "transcripts")
    settings = dict(config.DEFAULTS)
    settings["notesDir"] = str(tmp_path / "Notes")
    monkeypatch.setattr(config, "load_settings", lambda: dict(settings))
    fake = FakeLibrary(tmp_path)
    monkeypatch.setattr(library, "open_document", fake.open_document)
    fake.settings = settings
    fake.notes_dir = tmp_path / "Notes"
    return fake
