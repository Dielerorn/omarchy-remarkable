"""The ssh tar.zst backup."""

from __future__ import annotations

import json
import subprocess

import pytest

from rmbridge import cli, ssh
from rmbridge import tablet_remote as tr


def run_cli(argv, capsys) -> dict:
    code = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return {"code": code, **json.loads(out[-1])}


# ------------------------------------------------------------ backup


@pytest.fixture
def notebooks(sandbox):
    x = sandbox.path("/home/root/.local/share/remarkable/xochitl")
    (x / "aaaa").mkdir(parents=True)
    (x / "aaaa.metadata").write_text('{"visibleName": "Diary"}')
    (x / "aaaa/page.rm").write_bytes(b"reMarkable .lines file, version=6" + bytes(5000))
    return sandbox


def test_ssh_backup_streams_tar_zst_and_prunes(notebooks, isolated, capsys):
    folder = isolated / "backups" / "ssh"
    folder.mkdir(parents=True)
    for i in range(6):
        (folder / f"xochitl-2026010{i}-000000.tar.zst").write_bytes(b"old")
    (folder / "unrelated.txt").write_text("keep me")
    result = run_cli(["ssh", "backup"], capsys)
    assert result["ok"], result
    path = result["path"]
    assert path.startswith(str(folder)) and path.endswith(".tar.zst")
    listing = subprocess.run(f"zstd -dc '{path}' | tar -tf -", shell=True, capture_output=True, text=True).stdout
    assert "xochitl/aaaa.metadata" in listing and "xochitl/aaaa/page.rm" in listing
    kept = sorted(p.name for p in folder.glob("xochitl-*.tar.zst"))
    assert len(kept) == 5 and kept[-1] == path.rsplit("/", 1)[1]
    assert (folder / "unrelated.txt").exists() and not list(folder.glob("*.partial"))


def test_ssh_backup_failure_keeps_nothing(sandbox, isolated):
    # No xochitl folder: tar fails, nothing half-written is left behind.
    with pytest.raises(tr.TabletError):
        ssh.backup(isolated / "bk")
    assert list((isolated / "bk").iterdir()) == []
