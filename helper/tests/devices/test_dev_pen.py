"""Pen as a drawing tablet: device discovery, event translation, uinput."""

from __future__ import annotations

import fcntl
import io
import json
import os
import select
import subprocess
import time
from types import SimpleNamespace

import pytest

from conftest import busybox
from rmbridge import ssh
from rmbridge import tablet_pen as tp
from rmbridge import tablet_remote as tr

# /proc/bus/input/devices as a Paper Pro would print it (names and handler order from
# inkbridge's phase-0 dump; bitmaps derived from its evtest capability list).
PAPER_PRO_DEVICES = """I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="30370000.snvs:snvs-powerkey"
P: Phys=snvs-pwrkey/input0
H: Handlers=kbd event0
B: PROP=0
B: EV=3
B: KEY=10000000000000 0

I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="Hall effect sensors"
H: Handlers=event1
B: PROP=0
B: EV=21
B: SW=1

I: Bus=0018 Vendor=04f3 Product=0000 Version=0000
N: Name="Elan marker input"
H: Handlers=event2
B: PROP=0
B: EV=b
B: KEY=1c03 0 0 0 0 0
B: ABS=f000003

I: Bus=0018 Vendor=04f3 Product=0000 Version=0000
N: Name="Elan touch input"
H: Handlers=event3
B: PROP=2
B: EV=b
B: KEY=400 0 0 0 0 0
B: ABS=661800000000003
"""


def ev(t, c, v) -> bytes:
    return tp.REMOTE_EVENT.pack(1700000000, 123, t, c, v)


def test_find_paper_pro_pen():
    devices = tp.parse_input_devices(PAPER_PRO_DEVICES)
    assert [d.name for d in devices][2] == "Elan marker input"
    pen = tp.find_pen(devices)
    assert pen and pen.event_node == "/dev/input/event2"
    assert pen.has("KEY", tp.BTN_TOOL_PEN) and pen.has("KEY", tp.BTN_TOOL_RUBBER) and pen.has("KEY", tp.BTN_STYLUS2)
    assert pen.has("ABS", tp.ABS_TILT_Y) and not pen.has("ABS", 0x35)
    assert tp.profile_for(pen) is tp.PAPER_PRO


def test_find_pen_by_capabilities_when_renamed():
    text = PAPER_PRO_DEVICES.replace("Elan marker input", "Some digitizer")
    assert tp.find_pen(tp.parse_input_devices(text)).event_node == "/dev/input/event2"


@pytest.mark.parametrize("orientation,expected,tilt,axes", [
    ("portrait", (100, 200), (10, -20), (11180, 15340)),
    ("landscape", (15340 - 200, 100), (20, 10), (15340, 11180)),
    ("landscape-flipped", (200, 11180 - 100), (-20, -10), (15340, 11180)),
    ("portrait-flipped", (11180 - 100, 15340 - 200), (-10, 20), (11180, 15340)),
])
def test_orientation(orientation, expected, tilt, axes):
    m = tp.Mapper(tp.PAPER_PRO, orientation)
    m.x, m.y, m.tx, m.ty = 100, 200, 10, -20
    assert m.position() == expected and m.tilt() == tilt
    out = m.output_axes(tp.PAPER_PRO)
    assert (out[tp.ABS_X].maximum, out[tp.ABS_Y].maximum) == axes


def test_translate_absolute_report():
    t = tp.Translator(tp.Mapper(tp.PAPER_PRO, "portrait"))
    events = [(tp.EV_KEY, tp.BTN_TOOL_PEN, 1), (tp.EV_ABS, tp.ABS_X, 500), (tp.EV_ABS, tp.ABS_Y, 900),
              (tp.EV_ABS, tp.ABS_PRESSURE, 1200), (tp.EV_ABS, tp.ABS_TILT_X, 300), (tp.EV_KEY, tp.BTN_TOUCH, 1),
              (tp.EV_SYN, tp.SYN_REPORT, 0)]
    out = []
    for e in events:
        out += t.feed(*e)
    assert out == [(tp.EV_ABS, tp.ABS_X, 500), (tp.EV_ABS, tp.ABS_Y, 900),
                   (tp.EV_ABS, tp.ABS_TILT_X, 300), (tp.EV_ABS, tp.ABS_TILT_Y, 0),
                   (tp.EV_KEY, tp.BTN_TOOL_PEN, 1), (tp.EV_ABS, tp.ABS_PRESSURE, 1200),
                   (tp.EV_KEY, tp.BTN_TOUCH, 1), (tp.EV_SYN, tp.SYN_REPORT, 0)]
    assert t.tool == "pen"
    t.feed(tp.EV_KEY, tp.BTN_TOOL_RUBBER, 1)
    assert t.tool == "rubber"


def test_translate_relative_moves_like_a_mouse():
    t = tp.Translator(tp.Mapper(tp.PAPER_PRO, "portrait"), relative=True, speed=1.0)
    feed = lambda *evs: [o for e in evs for o in t.feed(*e)]
    assert feed((tp.EV_KEY, tp.BTN_TOOL_PEN, 1), (tp.EV_ABS, tp.ABS_X, 1000), (tp.EV_ABS, tp.ABS_Y, 1000),
                (tp.EV_SYN, 0, 0)) == []  # first sample only anchors
    out = feed((tp.EV_ABS, tp.ABS_X, 1100), (tp.EV_KEY, tp.BTN_TOUCH, 1), (tp.EV_SYN, 0, 0))
    assert out == [(tp.EV_REL, tp.REL_X, 15), (tp.EV_KEY, tp.BTN_LEFT, 1), (tp.EV_SYN, 0, 0)]
    # Lifting out of range forgets the anchor, so the next touch-down doesn't jump.
    feed((tp.EV_KEY, tp.BTN_TOOL_PEN, 0), (tp.EV_SYN, 0, 0))
    assert feed((tp.EV_KEY, tp.BTN_TOOL_PEN, 1), (tp.EV_ABS, tp.ABS_X, 9000), (tp.EV_SYN, 0, 0)) == []


def test_read_events_handles_split_reads():
    data = ev(3, 0, 42) + ev(3, 1, -7) + ev(0, 0, 0)

    class Trickle(io.RawIOBase):
        def __init__(self):
            self.chunks = [data[:10], data[10:30], data[30:]]

        def read1(self, n):
            return self.chunks.pop(0) if self.chunks else b""

    assert list(tp.read_events(Trickle())) == [(3, 0, 42), (3, 1, -7), (0, 0, 0)]


@pytest.mark.skipif(not busybox(), reason="needs BusyBox to check the tablet-side script")
def test_remote_scripts_parse_in_busybox():
    for script in (tp.remote_stream_script("/dev/input/event2"), tp.remote_cleanup_script([12, 13])):
        proc = subprocess.run([busybox(), "sh", "-n", "-c", script], capture_output=True)
        assert proc.returncode == 0, proc.stderr


uinput_ok = os.access("/dev/uinput", os.W_OK)
EVIOCGRAB = 0x40044590


@pytest.mark.skipif(not uinput_ok, reason="/dev/uinput not writable")
def test_virtual_tablet_appears_delivers_and_disappears():
    axes = tp.Mapper(tp.PAPER_PRO, "portrait").output_axes(tp.PAPER_PRO)
    name = f"omarchy-remarkable-test-{os.getpid()}"
    with tp.VirtualTablet(axes, name=name) as vt:
        assert vt.sysname and vt.sysname.startswith("input")
        assert f'N: Name="{name}"' in open("/proc/bus/input/devices").read()
        node = None
        for _ in range(40):
            node = vt.event_node()
            if node and os.access(node, os.R_OK):
                break
            time.sleep(0.05)
        assert node and os.access(node, os.R_OK)
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        try:
            # Grab it so the desktop never sees these test events.
            fcntl.ioctl(fd, EVIOCGRAB, 1)
            vt.emit([(tp.EV_ABS, tp.ABS_X, 4321), (tp.EV_ABS, tp.ABS_PRESSURE, 77), (tp.EV_SYN, 0, 0)])
            select.select([fd], [], [], 1.0)
            raw = os.read(fd, tp.LOCAL_EVENT.size * 8)
            got = [tp.LOCAL_EVENT.unpack_from(raw, o)[2:] for o in range(0, len(raw), tp.LOCAL_EVENT.size)]
            assert got == [(tp.EV_ABS, tp.ABS_X, 4321), (tp.EV_ABS, tp.ABS_PRESSURE, 77), (tp.EV_SYN, 0, 0)]
            fcntl.ioctl(fd, EVIOCGRAB, 0)
        finally:
            os.close(fd)
    time.sleep(0.1)
    assert f'N: Name="{name}"' not in open("/proc/bus/input/devices").read()


@pytest.mark.skipif(not uinput_ok, reason="/dev/uinput not writable")
def test_relative_device_creates_and_destroys():
    name = f"omarchy-remarkable-rel-{os.getpid()}"
    with tp.VirtualTablet(relative=True, name=name):
        assert f'N: Name="{name}"' in open("/proc/bus/input/devices").read()
    time.sleep(0.1)
    assert f'N: Name="{name}"' not in open("/proc/bus/input/devices").read()


class RecordingTablet:
    instances: list["RecordingTablet"] = []

    def __init__(self, axes=None, *, relative=False, name=tp.DEVICE_NAME, path="/dev/uinput"):
        self.axes, self.relative, self.name = axes, relative, name
        self.events: list = []
        self.destroyed = False
        RecordingTablet.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.destroyed = True

    def emit(self, events):
        self.events += events

    def event_node(self):
        return "/dev/input/event99"


class StreamingRemote(tr.Remote):
    def __init__(self, events_file):
        super().__init__("10.11.99.1")
        self.events_file = events_file
        self.ran: list[str] = []

    def run(self, script, **kw):
        self.ran.append(script)
        if "/proc/bus/input/devices" in script:
            return tr.Result(0, PAPER_PRO_DEVICES.encode(), b"")
        return tr.Result(0, b"", b"")

    def popen(self, script, **kw):
        assert "cat /dev/input/event2" in script and "wake_lock" in script
        return subprocess.Popen(["sh", "-c", f"echo 'pids 100 101 102' >&2; cat '{self.events_file}'"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_pen_start_streams_into_virtual_tablet(tmp_path, monkeypatch, capsys):
    stream = tmp_path / "events.bin"
    stream.write_bytes(ev(1, tp.BTN_TOOL_PEN, 1) + ev(3, 0, 100) + ev(3, 1, 200) + ev(0, 0, 0)
                       + ev(3, 24, 900) + ev(1, tp.BTN_TOUCH, 1) + ev(0, 0, 0)
                       + ev(1, tp.BTN_TOUCH, 0) + ev(1, tp.BTN_TOOL_PEN, 0) + ev(0, 0, 0))
    remote = StreamingRemote(stream)
    monkeypatch.setattr(tr, "factory", lambda host=None, user=None: remote)
    monkeypatch.setattr(tp, "VirtualTablet", RecordingTablet)
    RecordingTablet.instances.clear()
    args = SimpleNamespace(orientation="landscape", mode="absolute", speed=1.0, max_x=None, max_y=None,
                           max_pressure=None)
    result = ssh.cmd_pen_start(args)
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert args.streaming and result["ok"]
    assert lines[0]["event"] == "started" and lines[0]["device"] == tp.DEVICE_NAME
    assert lines[0]["axes"]["0"] == [0, 15340]  # landscape swaps the axes
    assert {"event": "tool", "tool": "pen"} in lines and lines[-1]["event"] == "stopped"
    vt = RecordingTablet.instances[0]
    assert vt.destroyed
    assert (tp.EV_ABS, tp.ABS_X, 15340 - 200) in vt.events and (tp.EV_ABS, tp.ABS_Y, 100) in vt.events
    assert (tp.EV_KEY, tp.BTN_TOUCH, 1) in vt.events and (tp.EV_ABS, tp.ABS_PRESSURE, 900) in vt.events
    assert any("wake_unlock" in s and "kill 101 102" in s for s in remote.ran)
    assert not ssh._pen_file().exists()
    assert ssh.cmd_pen_status(SimpleNamespace())["running"] is False
