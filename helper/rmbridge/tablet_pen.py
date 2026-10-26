"""Use the tablet's Marker as a drawing tablet on this computer.

The tablet streams raw evdev events from its pen digitizer (`cat
/dev/input/eventN` over ssh), and they are replayed on a local virtual tablet
created through /dev/uinput (root:input 0660; the user is in `input`).

Paper Pro facts (KOReader frontend/device/remarkable/device.lua; inkbridge
docs/phase0-findings.md, an evtest dump from a Paper Pro):
  event2 "Elan marker input": ABS_X 0..11180 (short edge), ABS_Y 0..15340,
  ABS_PRESSURE 0..4096, ABS_DISTANCE 0..65535, ABS_TILT_X/Y -9000..9000,
  BTN_TOOL_PEN, BTN_TOOL_RUBBER, BTN_TOUCH, BTN_STYLUS(2). Axes line up with
  the portrait screen (origin top-left). The tablet suspends itself when no
  wake lock is held and the digitizer then goes silent, so the remote side
  holds a short, self-renewing wake lock that lapses within ~15 s if this
  side disappears.
The device has no evtest/python, so axis ranges can't be read remotely; they
come from the table below and can be overridden on the command line.
"""

from __future__ import annotations

import fcntl
import os
import struct
from dataclasses import dataclass, field

# ------------------------------------------------------------ evdev codes

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
SYN_REPORT = 0
ABS_X, ABS_Y, ABS_PRESSURE, ABS_DISTANCE, ABS_TILT_X, ABS_TILT_Y = 0x00, 0x01, 0x18, 0x19, 0x1A, 0x1B
REL_X, REL_Y = 0x00, 0x01
BTN_LEFT, BTN_RIGHT = 0x110, 0x111
BTN_TOOL_PEN, BTN_TOOL_RUBBER, BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2 = 0x140, 0x141, 0x14A, 0x14B, 0x14C
INPUT_PROP_POINTER = 0x00
BUS_VIRTUAL = 0x06

# ------------------------------------------------------------ uinput ioctls (linux/uinput.h)

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503  # _IOW('U', 3, struct uinput_setup)       (92 bytes)
UI_ABS_SETUP = 0x401C5504  # _IOW('U', 4, struct uinput_abs_setup)   (28 bytes)
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E


def UI_GET_SYSNAME(length: int) -> int:
    return (2 << 30) | (length << 16) | (ord("U") << 8) | 44


# The tablet is aarch64: struct input_event = timeval (2 x i64) + u16 type + u16 code + i32 value.
REMOTE_EVENT = struct.Struct("<qqHHi")
# This computer's layout (timeval is two C longs).
LOCAL_EVENT = struct.Struct("@llHHi")

DEVICE_NAME = "omarchy-remarkable-pen"
WAKE_LOCK = "omarchy_remarkable_pen"


@dataclass
class Axis:
    minimum: int
    maximum: int
    resolution: int = 0  # units/mm for X/Y, units/radian for tilt


@dataclass
class PenProfile:
    name: str
    axes: dict[int, Axis] = field(default_factory=dict)


# 157 x 210 mm active area (inkbridge's estimate) gives ~71 and ~73 units/mm.
PAPER_PRO = PenProfile("Elan marker input", {
    ABS_X: Axis(0, 11180, 71), ABS_Y: Axis(0, 15340, 73), ABS_PRESSURE: Axis(0, 4096),
    ABS_DISTANCE: Axis(0, 65535), ABS_TILT_X: Axis(-9000, 9000, 5730), ABS_TILT_Y: Axis(-9000, 9000, 5730),
})
# reMarkable 2 Wacom digitizer (libremarkable/KOReader): axes are swapped relative to the screen there,
# which this table doesn't model; it only serves as a sane fallback.
REMARKABLE_2 = PenProfile("Wacom I2C Digitizer", {
    ABS_X: Axis(0, 20967, 100), ABS_Y: Axis(0, 15725, 100), ABS_PRESSURE: Axis(0, 4095),
    ABS_DISTANCE: Axis(0, 255), ABS_TILT_X: Axis(-9000, 9000, 5730), ABS_TILT_Y: Axis(-9000, 9000, 5730),
})


# ------------------------------------------------------------ remote side


@dataclass
class InputDevice:
    name: str
    handlers: list[str]
    bits: dict[str, list[int]]

    @property
    def event_node(self) -> str | None:
        for h in self.handlers:
            if h.startswith("event"):
                return f"/dev/input/{h}"
        return None

    def has(self, kind: str, code: int) -> bool:
        words = self.bits.get(kind, [])
        word, bit = divmod(code, 64)
        # /proc prints the bitmap as longs, most significant first.
        idx = len(words) - 1 - word
        return 0 <= idx < len(words) and bool((words[idx] >> bit) & 1)


def parse_input_devices(text: str) -> list[InputDevice]:
    devices: list[InputDevice] = []
    for block in text.strip().split("\n\n"):
        name, handlers, bits = "", [], {}
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
            elif line.startswith("B: "):
                key, _, value = line[3:].partition("=")
                try:
                    bits[key.strip()] = [int(w, 16) for w in value.split()]
                except ValueError:
                    pass
        if name:
            devices.append(InputDevice(name, handlers, bits))
    return devices


def find_pen(devices: list[InputDevice]) -> InputDevice | None:
    """The pen digitizer: by name first, then by capabilities."""
    for dev in devices:
        lowered = dev.name.lower()
        if ("marker" in lowered or "wacom" in lowered or "stylus" in lowered) and dev.event_node:
            return dev
    for dev in devices:
        if dev.event_node and dev.has("KEY", BTN_TOOL_PEN) and dev.has("ABS", ABS_X):
            return dev
    return None


def profile_for(dev: InputDevice) -> PenProfile:
    if "wacom" in dev.name.lower():
        return REMARKABLE_2
    return PAPER_PRO


def remote_stream_script(node: str) -> str:
    """Shell run on the tablet: hold a lapsing wake lock and copy raw events to stdout.

    The heartbeat goes to stderr; once this computer hangs up, the write fails,
    the loop dies, and the 15 s lock simply expires.
    """
    return f"""
lock() {{ echo "{WAKE_LOCK} 15000000000" > /sys/power/wake_lock 2>/dev/null; }}
( while lock; do printf . >&2 || exit; sleep 5; done ) &
beat=$!
cat {node} &
reader=$!
echo "pids $$ $reader $beat" >&2
cleanup() {{ kill $reader $beat 2>/dev/null; echo {WAKE_LOCK} > /sys/power/wake_unlock 2>/dev/null; }}
trap cleanup EXIT INT TERM HUP PIPE
wait $reader
"""


def remote_cleanup_script(pids: list[int]) -> str:
    kills = " ".join(str(p) for p in pids if p > 1)
    return (f"kill {kills} 2>/dev/null; " if kills else "") + f"echo {WAKE_LOCK} > /sys/power/wake_unlock 2>/dev/null; true"


# ------------------------------------------------------------ geometry


ORIENTATIONS = ("portrait", "landscape", "portrait-flipped", "landscape-flipped")


class Mapper:
    """Rotates pen coordinates for the way the tablet is held.

    landscape = tablet turned 90° clockwise (its top edge on the right).
    """

    def __init__(self, profile: PenProfile, orientation: str = "portrait"):
        if orientation not in ORIENTATIONS:
            raise ValueError(f"orientation must be one of {', '.join(ORIENTATIONS)}")
        self.orientation = orientation
        self.xmax = profile.axes[ABS_X].maximum
        self.ymax = profile.axes[ABS_Y].maximum
        self.swap = orientation in ("landscape", "landscape-flipped")
        self.x = 0
        self.y = 0
        self.tx = 0
        self.ty = 0

    def output_axes(self, profile: PenProfile) -> dict[int, Axis]:
        axes = dict(profile.axes)
        if self.swap:
            axes[ABS_X], axes[ABS_Y] = profile.axes[ABS_Y], profile.axes[ABS_X]
            axes[ABS_TILT_X], axes[ABS_TILT_Y] = profile.axes.get(ABS_TILT_Y), profile.axes.get(ABS_TILT_X)
            axes = {k: v for k, v in axes.items() if v is not None}
        return axes

    def position(self) -> tuple[int, int]:
        x, y = self.x, self.y
        if self.orientation == "landscape":
            return self.ymax - y, x
        if self.orientation == "landscape-flipped":
            return y, self.xmax - x
        if self.orientation == "portrait-flipped":
            return self.xmax - x, self.ymax - y
        return x, y

    def tilt(self) -> tuple[int, int]:
        tx, ty = self.tx, self.ty
        if self.orientation == "landscape":
            return -ty, tx
        if self.orientation == "landscape-flipped":
            return ty, -tx
        if self.orientation == "portrait-flipped":
            return -tx, -ty
        return tx, ty


# ------------------------------------------------------------ local uinput device


class VirtualTablet:
    """A uinput device. Use as a context manager so it's always destroyed."""

    def __init__(self, axes: dict[int, Axis] | None = None, *, relative: bool = False,
                 name: str = DEVICE_NAME, path: str = "/dev/uinput"):
        self.axes = axes or {}
        self.relative = relative
        self.name = name
        self.path = path
        self.fd: int | None = None
        self.sysname: str | None = None

    def __enter__(self) -> "VirtualTablet":
        self.create()
        return self

    def __exit__(self, *exc) -> None:
        self.destroy()

    def create(self) -> None:
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError as e:
            raise PermissionError("can't open /dev/uinput; your user needs to be in the input group") from e
        except FileNotFoundError as e:
            raise FileNotFoundError("/dev/uinput is missing (the uinput kernel module isn't loaded)") from e
        self.fd = fd
        try:
            if self.relative:
                self._ioctl(UI_SET_EVBIT, EV_KEY)
                self._ioctl(UI_SET_EVBIT, EV_REL)
                for key in (BTN_LEFT, BTN_RIGHT):
                    self._ioctl(UI_SET_KEYBIT, key)
                for rel in (REL_X, REL_Y):
                    self._ioctl(UI_SET_RELBIT, rel)
                self._ioctl(UI_SET_PROPBIT, INPUT_PROP_POINTER)
            else:
                self._ioctl(UI_SET_EVBIT, EV_KEY)
                self._ioctl(UI_SET_EVBIT, EV_ABS)
                for key in (BTN_TOOL_PEN, BTN_TOOL_RUBBER, BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2):
                    self._ioctl(UI_SET_KEYBIT, key)
                # No INPUT_PROP_DIRECT: this is a tablet you look away from, mapped to a monitor.
                self._ioctl(UI_SET_PROPBIT, INPUT_PROP_POINTER)
                for code, axis in self.axes.items():
                    self._ioctl(UI_SET_ABSBIT, code)
                    setup = struct.pack("<H2x6i", code, 0, axis.minimum, axis.maximum, 0, 0, axis.resolution)
                    fcntl.ioctl(fd, UI_ABS_SETUP, setup)
            name = self.name.encode()[:79]
            setup = struct.pack("<HHHH80sI", BUS_VIRTUAL, 0x2D1F, 0x0001, 1, name, 0)
            fcntl.ioctl(fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(fd, UI_DEV_CREATE)
            buf = bytearray(64)
            try:
                fcntl.ioctl(fd, UI_GET_SYSNAME(len(buf)), buf)
                self.sysname = buf.split(b"\0", 1)[0].decode() or None
            except OSError:
                self.sysname = None
        except BaseException:
            os.close(fd)
            self.fd = None
            raise

    def _ioctl(self, request: int, value: int) -> None:
        assert self.fd is not None
        fcntl.ioctl(self.fd, request, value)

    def emit(self, events: list[tuple[int, int, int]]) -> None:
        if self.fd is None or not events:
            return
        os.write(self.fd, b"".join(LOCAL_EVENT.pack(0, 0, t, c, v) for t, c, v in events))

    def event_node(self) -> str | None:
        """/dev/input/eventN of the created device, found through sysfs."""
        if not self.sysname:
            return None
        base = f"/sys/devices/virtual/input/{self.sysname}"
        try:
            for entry in os.listdir(base):
                if entry.startswith("event"):
                    return f"/dev/input/{entry}"
        except OSError:
            pass
        return None

    def destroy(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None


# ------------------------------------------------------------ translation


class Translator:
    """Turns the tablet's event stream into events for the local device."""

    KEYS = {BTN_TOOL_PEN, BTN_TOOL_RUBBER, BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2}

    def __init__(self, mapper: Mapper, *, relative: bool = False, speed: float = 1.0):
        self.mapper = mapper
        self.relative = relative
        self.scale = 0.15 * speed  # ~1700 px across the tablet's width
        self.pending: list[tuple[int, int, int]] = []
        self.moved = False
        self.tilted = False
        self.last_rel: tuple[int, int] | None = None
        self.carry = [0.0, 0.0]
        self.in_range = False
        self.rubber = False
        self.events = 0
        self.tool: str | None = None

    def feed(self, etype: int, code: int, value: int) -> list[tuple[int, int, int]]:
        """Consume one remote event; return local events when a report completes."""
        self.events += 1
        if etype == EV_ABS:
            if code == ABS_X:
                self.mapper.x, self.moved = value, True
            elif code == ABS_Y:
                self.mapper.y, self.moved = value, True
            elif code == ABS_TILT_X:
                self.mapper.tx, self.tilted = value, True
            elif code == ABS_TILT_Y:
                self.mapper.ty, self.tilted = value, True
            elif code in (ABS_PRESSURE, ABS_DISTANCE) and not self.relative:
                self.pending.append((EV_ABS, code, value))
        elif etype == EV_KEY and code in self.KEYS:
            if code == BTN_TOOL_PEN:
                self.in_range = bool(value) or self.rubber
                self.tool = "pen" if value else (self.tool if self.rubber else None)
            elif code == BTN_TOOL_RUBBER:
                self.rubber = bool(value)
                self.in_range = bool(value)
                self.tool = "rubber" if value else None
            if self.relative:
                if code == BTN_TOUCH:
                    self.pending.append((EV_KEY, BTN_RIGHT if self.rubber else BTN_LEFT, value))
                if code in (BTN_TOOL_PEN, BTN_TOOL_RUBBER) and not value:
                    self.last_rel = None
            else:
                self.pending.append((EV_KEY, code, value))
        elif etype == EV_SYN and code == SYN_REPORT:
            return self.flush()
        return []

    def flush(self) -> list[tuple[int, int, int]]:
        out: list[tuple[int, int, int]] = []
        if self.relative:
            if self.moved:
                x, y = self.mapper.position()
                if self.last_rel is not None and self.in_range:
                    self.carry[0] += (x - self.last_rel[0]) * self.scale
                    self.carry[1] += (y - self.last_rel[1]) * self.scale
                    dx, dy = int(self.carry[0]), int(self.carry[1])
                    self.carry = [self.carry[0] - dx, self.carry[1] - dy]
                    if dx:
                        out.append((EV_REL, REL_X, dx))
                    if dy:
                        out.append((EV_REL, REL_Y, dy))
                self.last_rel = (x, y) if self.in_range else None
        else:
            if self.moved:
                x, y = self.mapper.position()
                out += [(EV_ABS, ABS_X, x), (EV_ABS, ABS_Y, y)]
            if self.tilted:
                tx, ty = self.mapper.tilt()
                out += [(EV_ABS, ABS_TILT_X, tx), (EV_ABS, ABS_TILT_Y, ty)]
        out += self.pending
        self.pending = []
        self.moved = self.tilted = False
        if out:
            out.append((EV_SYN, SYN_REPORT, 0))
        return out


def read_events(stream, chunk: int = 4096):
    """Yield (type, code, value) from a stream of the tablet's input_event structs."""
    buf = b""
    size = REMOTE_EVENT.size
    while True:
        data = stream.read1(chunk) if hasattr(stream, "read1") else stream.read(chunk)
        if not data:
            return
        buf += data
        usable = len(buf) - len(buf) % size
        for off in range(0, usable, size):
            _, _, etype, code, value = REMOTE_EVENT.unpack_from(buf, off)
            yield etype, code, value
        buf = buf[usable:]


def hyprland_hint(name: str = DEVICE_NAME) -> dict:
    return {
        "device": name,
        "mapToMonitor": f"a Hyprland device section with name = {name} and output = <monitor, e.g. DP-1>; "
                        "or input:tablet:output = <monitor> for every tablet",
        "keepAspect": "input:tablet:region_size / region_position crop the monitor area so a 3:4 tablet "
                      "isn't stretched across a 16:9 screen",
        "relative": "input:tablet:relative_input = true makes the pen move the cursor like a mouse",
    }
