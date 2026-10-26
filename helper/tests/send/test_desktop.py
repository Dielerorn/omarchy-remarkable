"""Open With entry and command link: added on request, removed only if ours."""

from __future__ import annotations

import os

from rmbridge import desktop


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("OMARCHY_REMARKABLE_PLUGIN", str(tmp_path / "plugin"))
    return home


def test_add_and_remove(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    assert desktop.status()["openWith"] is False
    r = desktop.add()
    assert r["ok"] and r["openWith"] and r["command"]
    entry = (home / ".local/share/applications/omarchy-remarkable-send.desktop").read_text()
    assert f'Exec="{tmp_path}/plugin/bin/omarchy-remarkable" send --notify %F' in entry
    assert os.readlink(home / ".local/bin/omarchy-remarkable") == f"{tmp_path}/plugin/bin/omarchy-remarkable"
    r = desktop.remove()
    assert len(r["removed"]) == 2 and not r["openWith"] and not r["command"]


def test_only_one_part(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    desktop.add(open_with=True, command=False)
    assert desktop.status()["openWith"] and not (home / ".local/bin/omarchy-remarkable").exists()


def test_leaves_other_files_alone(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    (home / ".local/bin").mkdir(parents=True)
    other = home / ".local/bin/omarchy-remarkable"
    other.write_text("#!/bin/sh\necho mine\n")
    st = desktop.status()
    assert st["commandTaken"] and not st["command"]
    assert desktop.add(open_with=False)["ok"] is False
    desktop.remove()
    assert other.read_text() == "#!/bin/sh\necho mine\n"
