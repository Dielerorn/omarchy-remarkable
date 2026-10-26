"""Sleep screen and the remount helper, run as real shell on a sandboxed 'tablet'."""

from __future__ import annotations

import json

import pymupdf
import pytest

from rmbridge import cli
from rmbridge import tablet_system as ts
from rmbridge.tablet_remote import TabletError


def make_png(path, w, h, value=180):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.clear_with(value)
    pix.save(path)
    return path


@pytest.fixture
def tablet(sandbox):
    share = sandbox.path("/usr/share/remarkable")
    (share / "carousel").mkdir(parents=True)
    make_png(share / "suspended.png", 1620, 2160, 255)
    for i in (1, 2):
        make_png(share / "carousel" / f"sleep_Illustration_{i}.png", 1620, 2160, 100 + i)
    (share / "carousel" / "other.png").write_bytes(b"x")
    sandbox.lock_rootfs()
    return sandbox


def run_cli(argv, capsys) -> dict:
    code = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return {"code": code, **json.loads(out[-1])}


def test_firmware_from_os_release(tablet):
    assert ts.firmware(tablet) == "3.27.3.0"
    assert ts.parse_firmware("@@REMARKABLE_RELEASE_VERSION=3.5.2.1807\n@@20230101") == "3.5.2.1807"
    assert ts.parse_firmware("@@@@20230101\n") == "20230101"


def test_fit_png_letterboxes_to_screen(tmp_path):
    src = make_png(tmp_path / "wide.png", 400, 100)
    data, info = ts.fit_png(src)
    assert ts.png_size(data) == (1620, 2160) and info["converted"] and info["source"] == [400, 100]
    exact = make_png(tmp_path / "exact.png", 1620, 2160)
    data, info = ts.fit_png(exact)
    assert data == exact.read_bytes() and info["converted"] is False


def test_sleepscreen_needs_yes(tablet, tmp_path, capsys):
    img = make_png(tmp_path / "pic.png", 800, 600)
    result = run_cli(["ssh", "sleepscreen", "set", str(img)], capsys)
    assert result["ok"] is False and result["code"] == "needs-confirmation"
    assert any("resized from 800×600" in step for step in result["plan"])
    assert "firmware update" in result["warning"]
    assert tablet.calls() == []  # nothing ran on the tablet


def test_sleepscreen_set_status_restore(tablet, tmp_path, capsys, isolated):
    share = tablet.path("/usr/share/remarkable")
    stock = (share / "suspended.png").read_bytes()
    img = make_png(tmp_path / "pic.png", 800, 600, 30)

    before = run_cli(["ssh", "sleepscreen", "status"], capsys)
    assert before["state"] == "stock" and before["backupExists"] is False
    assert len(before["carousel"]) == 2

    result = run_cli(["ssh", "sleepscreen", "set", str(img), "--yes"], capsys)
    assert result["ok"], result
    assert result["replaced"] == ["/usr/share/remarkable/suspended.png"] and result["rootReadOnly"]
    assert result["restarted"] is False and "--restart" in result["note"]
    new = (share / "suspended.png").read_bytes()
    assert new != stock and ts.png_size(new) == (1620, 2160)
    # Originals kept on the tablet (/home) and here, scoped to the firmware.
    assert tablet.path("/home/root/.omarchy-remarkable-backup/3.27.3.0/usr/share/remarkable/suspended.png").read_bytes() == stock
    assert (isolated / "data/tablet-backup/3.27.3.0/usr/share/remarkable/suspended.png").read_bytes() == stock
    # The window was opened and closed again, and / is read-only afterwards.
    assert tablet.calls() == ["mount -o remount,rw /", "mount -o remount,ro /"]
    assert " / ext4 ro," in tablet.path("/proc/mounts").read_text()
    assert not tablet.path("/home/root/.omarchy-remarkable/staging").exists()

    status = run_cli(["ssh", "sleepscreen", "status"], capsys)
    assert status["state"] == "custom" and status["backupExists"]

    # A second set must not overwrite the stock backup with our picture.
    img2 = make_png(tmp_path / "pic2.png", 1620, 2160, 60)
    assert run_cli(["ssh", "sleepscreen", "set", str(img2), "--yes", "--restart"], capsys)["restarted"]
    assert "systemctl restart xochitl" in tablet.calls()
    assert (isolated / "data/tablet-backup/3.27.3.0/usr/share/remarkable/suspended.png").read_bytes() == stock

    restored = run_cli(["ssh", "sleepscreen", "restore", "--yes"], capsys)
    assert restored["ok"] and restored["restored"] == ["/usr/share/remarkable/suspended.png"]
    assert (share / "suspended.png").read_bytes() == stock
    assert run_cli(["ssh", "sleepscreen", "status"], capsys)["state"] == "stock"
    again = run_cli(["ssh", "sleepscreen", "restore", "--yes"], capsys)
    assert again["ok"] and again["restored"] == [] and "Already" in again["note"]


def test_sleepscreen_with_carousel(tablet, tmp_path):
    share = tablet.path("/usr/share/remarkable")
    originals = {p.name: p.read_bytes() for p in (share / "carousel").glob("sleep_*.png")}
    data, _, _ = ts.sleepscreen_plan(make_png(tmp_path / "p.png", 1620, 2160, 5), carousel=True)
    result = ts.sleepscreen_set(tablet, data, carousel=True, restart=False)
    assert len(result["replaced"]) == 3
    assert all((share / "carousel" / n).read_bytes() == data for n in originals)
    assert (share / "carousel" / "other.png").read_bytes() == b"x"
    assert ts.sleepscreen_status(tablet)["carouselChanged"]
    ts.sleepscreen_restore(tablet, restart=False)
    assert {n: (share / "carousel" / n).read_bytes() for n in originals} == originals


def test_restore_without_backup_changes_nothing(tablet):
    result = ts.sleepscreen_restore(tablet, restart=False)
    assert result["applied"] is False and "already" in result["note"].lower()
    assert tablet.calls() == []


def test_apply_refuses_paths_outside_remarkable_share(tablet):
    with pytest.raises(TabletError):
        ts.apply(tablet, [ts.Change("/etc/passwd", b"x")], restart=False)


def test_apply_leaves_rw_root_alone(tablet):
    tablet.unlock_rootfs()
    mounts = tablet.path("/proc/mounts")
    mounts.write_text(mounts.read_text().replace(" / ext4 ro,", " / ext4 rw,"))
    ts.apply(tablet, [ts.Change("/usr/share/remarkable/x.txt", b"hi")], restart=False)
    assert tablet.calls() == []  # it was writable already: no remounts either way
    assert tablet.path("/usr/share/remarkable/x.txt").read_bytes() == b"hi"


def test_put_rejects_damaged_upload(tablet, monkeypatch):
    real = tablet.rewrite
    monkeypatch.setattr(tablet, "rewrite", lambda s: real(s).replace("cat >", "head -c 3 >"))
    with pytest.raises(TabletError, match="damaged"):
        tablet.put(b"hello world", "/home/root/x.bin")
    assert not tablet.path("/home/root/x.bin").exists()
    assert not tablet.path("/home/root/x.bin.part").exists()
