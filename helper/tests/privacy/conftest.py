"""Fixtures for the process-list privacy tests: config paths go to tmp_path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from rmbridge import config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name, sub in {"CONFIG_DIR": "config", "CACHE_DIR": "cache", "DATA_DIR": "data", "RUNTIME_DIR": "run"}.items():
        monkeypatch.setattr(config, name, tmp_path / sub)
    monkeypatch.setattr(config, "RMAPI_CONFIG", tmp_path / "config" / "rmapi.conf")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "cache" / "tmp")
    config.ensure_dirs()
    return tmp_path
