"""Private document data stays owner-only: folders are 0700, files 0600, and
an upgrade tightens what an older version wrote with the default umask.
Where user namespaces allow it, reads are tried as a second account."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from rmbridge import cli, cloud, config, search


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def base(monkeypatch):
    """App folders under a world-traversable /tmp directory (pytest's own tmp
    is owner-only, which would hide the problem being tested)."""
    root = Path(tempfile.mkdtemp(prefix="omr-perm-", dir="/tmp"))
    root.chmod(0o755)
    dirs = {"CONFIG_DIR": root / "config", "CACHE_DIR": root / "cache", "DATA_DIR": root / "data",
            "RUNTIME_DIR": root / "run"}
    for name, path in dirs.items():
        monkeypatch.setattr(config, name, path)
    for name, path in {"SETTINGS_FILE": dirs["CONFIG_DIR"] / "settings.json",
                       "INDEX_DB": dirs["DATA_DIR"] / "index.db", "DOC_CACHE": dirs["CACHE_DIR"] / "docs",
                       "RENDER_CACHE": dirs["CACHE_DIR"] / "renders", "THUMB_CACHE": dirs["CACHE_DIR"] / "thumbs",
                       "TMP_DIR": dirs["CACHE_DIR"] / "tmp"}.items():
        monkeypatch.setattr(config, name, path)
    old = os.umask(0o022)
    yield root
    os.umask(old)
    shutil.rmtree(root, ignore_errors=True)


def _other_account(path: Path) -> subprocess.CompletedProcess:
    """`cat path` as UID 100000 (a subordinate ID, not this user)."""
    return subprocess.run(["unshare", "--user", "--map-users=100000,0,1", "--map-groups=100000,0,1",
                           "--setuid", "0", "--setgid", "0", "cat", str(path)], capture_output=True)


@pytest.fixture
def other_can_read(base):
    probe = base / "probe.txt"
    probe.write_text("x")
    probe.chmod(0o644)
    if not shutil.which("unshare") or _other_account(probe).returncode != 0:
        pytest.skip("can't run a process as another UID here (user namespaces / newuidmap)")
    return lambda p: _other_account(p).returncode == 0


def _old_layout(base: Path) -> list[Path]:
    """What 0.1.x wrote under umask 022."""
    files = {
        config.DATA_DIR / "index.db": b"SQLite format 3\0private text",
        config.DATA_DIR / "index.db-wal": b"wal",
        config.DATA_DIR / "index.db-shm": b"shm",
        config.DATA_DIR / "transcripts" / "doc1" / "p1.md": b"my handwriting",
        config.DATA_DIR / "library.json": b'{"names": ["Diary"]}',
        config.DOC_CACHE / "doc1" / "abc.rmdoc": b"PK private notebook",
        config.RENDER_CACHE / "doc1" / "0001.png": b"\x89PNG page image",
        config.SETTINGS_FILE: b"{}",
    }
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o644)
    for d in {p.parent for p in files} | set(config.private_dirs()):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o755)
    return list(files)


def test_upgrade_tightens_old_files(base, other_can_read):
    files = _old_layout(base)
    outside = base / "outside.txt"
    outside.write_text("not ours")
    outside.chmod(0o644)
    (config.DATA_DIR / "venv").mkdir(mode=0o755)
    (config.DATA_DIR / "venv" / "python").symlink_to(outside)
    assert all(other_can_read(f) for f in files)  # the problem as reported

    config.ensure_dirs()
    assert config.tighten_private() >= len(files)

    assert not any(other_can_read(f) for f in files)
    for f in files:
        assert _mode(f) == 0o600, f
    for d in config.private_dirs():
        assert _mode(d) == 0o700
    assert _mode(outside) == 0o644  # symlinks aren't followed


def test_new_files_are_owner_only(base, monkeypatch):
    assert cli.main(["settings", "show"]) == 0  # sets umask 077, creates the folders
    assert os.umask(0o077) == 0o077
    for d in config.private_dirs():
        assert _mode(d) == 0o700
    con = search.connect()
    con.execute("CREATE TABLE IF NOT EXISTS t (x)")
    con.execute("INSERT INTO t VALUES ('private')")
    con.commit()
    for p in config.DATA_DIR.glob("index.db*"):
        assert _mode(p) == 0o600, p
    con.close()


def test_new_install_as_another_account(base, other_can_read):
    os.umask(0o077)
    config.ensure_dirs()
    con = search.connect()
    con.execute("CREATE TABLE IF NOT EXISTS t (x)")
    con.commit()
    con.close()
    assert not other_can_read(config.INDEX_DB)


def test_downloaded_documents_are_owner_only(base, monkeypatch):
    config.ensure_dirs()

    def fake_get(args, cwd=None, **kw):  # rmapi writes its output with 0644
        out = Path(cwd) / "doc.rmdoc"
        out.write_bytes(b"PK")
        out.chmod(0o644)
        return ""
    monkeypatch.setattr(cloud, "run", fake_get)
    entry = cloud.Entry(id="doc1", name="Diary", is_folder=False, parent="", modified="2026-10-29")
    path = cloud.download(entry)
    assert _mode(path) == 0o600


def test_runtime_dir_never_falls_back_to_shared_tmp(monkeypatch):
    import importlib
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    fresh = importlib.reload(config)
    try:
        assert not str(fresh.RUNTIME_DIR).startswith(tempfile.gettempdir())
        assert fresh.RUNTIME_DIR == fresh.CACHE_DIR / "run" / "omarchy-remarkable"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
