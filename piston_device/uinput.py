"""Virtual pointer and keyboard through /dev/uinput for remote input (stdlib only).

Ported from the standalone Remote Control plugin. The pointer is a relative mouse (every compositor treats it as a normal
one; an absolute pointer does nothing in Steam), the keyboard types ASCII and the named keys. The media-key keyboard in
`media_keys.py` is a separate device and is not touched by this module."""
import fcntl
import os
import struct
import time

EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
SYN_REPORT = 0
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566

BUS_VIRTUAL = 0x06
UINPUT_PATH = "/dev/uinput"
POINTER_NAME = "Piston Pointer"
KEYBOARD_NAME = "Piston Keyboard"

# Linux key codes for the named keys the protocol allows (messages.INPUT_KEYS)
KEY = {
    "Escape": 1, "Backspace": 14, "Tab": 15, "Enter": 28, "Ctrl": 29, "Shift": 42,
    "Space": 57, "Home": 102, "Up": 103, "PageUp": 104, "Left": 105, "Right": 106,
    "End": 107, "Down": 108, "PageDown": 109, "Delete": 111,
}
_ROW = {
    **{c: 2 + i for i, c in enumerate("1234567890")},
    "-": 12, "=": 13,
    **{c: 16 + i for i, c in enumerate("qwertyuiop")},
    "[": 26, "]": 27,
    **{c: 30 + i for i, c in enumerate("asdfghjkl")},
    ";": 39, "'": 40, "`": 41, "\\": 43,
    **{c: 44 + i for i, c in enumerate("zxcvbnm")},
    ",": 51, ".": 52, "/": 53, " ": 57,
}
_SHIFT = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", ":": ";", '"': "'",
    "~": "`", "|": "\\", "<": ",", ">": ".", "?": "/",
}


def char_to_key(ch: str):
    """(keycode, needs_shift) for an ASCII character, or None when it has no key."""
    if ch == "\n":
        return KEY["Enter"], False
    if ch == "\t":
        return KEY["Tab"], False
    if ch in _ROW:
        return _ROW[ch], False
    if ch.isascii() and ch.isalpha() and ch.isupper():
        return _ROW[ch.lower()], True
    if ch in _SHIFT:
        return _ROW[_SHIFT[ch]], True
    return None


def pack_event(etype: int, code: int, value: int, ts: float | None = None) -> bytes:
    t = time.time() if ts is None else ts
    return struct.pack("llHHi", int(t), int((t % 1) * 1_000_000), etype, code, value)


class Device:
    """A uinput device. `path` is injectable so tests can point it at a temp file (and patch `fcntl.ioctl`)."""

    def __init__(self, name: str, keys=(), rels=(), path: str = UINPUT_PATH):
        self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        self._keys = tuple(keys)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for key in self._keys:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)
            if rels:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                for rel in rels:
                    fcntl.ioctl(self.fd, UI_SET_RELBIT, rel)
            setup = struct.pack("HHHH80sI", BUS_VIRTUAL, 0x1209, 0xDE01, 1, name.encode()[:79], 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except Exception:
            os.close(self.fd)
            raise

    def emit(self, *events) -> None:
        """Write the events plus the closing sync. A full queue drops the batch (input is best effort)."""
        buf = b"".join(pack_event(t, c, v) for t, c, v in events) + pack_event(EV_SYN, SYN_REPORT, 0)
        try:
            os.write(self.fd, buf)
        except BlockingIOError:
            pass

    def release_all(self) -> None:
        """Key-up for everything this device can press. Keys that are not down ignore it, so this is always safe."""
        if self._keys:
            self.emit(*[(EV_KEY, key, 0) for key in self._keys])

    def close(self) -> None:
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


class Pointer(Device):
    """Standard relative mouse."""

    def __init__(self, path: str = UINPUT_PATH):
        super().__init__(POINTER_NAME, keys=(BTN_LEFT, BTN_RIGHT, BTN_MIDDLE),
                         rels=(REL_X, REL_Y, REL_WHEEL, REL_HWHEEL), path=path)

    def move(self, dx: int, dy: int) -> None:
        events = []
        if dx:
            events.append((EV_REL, REL_X, int(dx)))
        if dy:
            events.append((EV_REL, REL_Y, int(dy)))
        if events:
            self.emit(*events)

    def button(self, button: int, down: bool) -> None:
        self.emit((EV_KEY, button, 1 if down else 0))

    def scroll(self, vertical: int, horizontal: int = 0) -> None:
        events = []
        if vertical:
            events.append((EV_REL, REL_WHEEL, int(vertical)))
        if horizontal:
            events.append((EV_REL, REL_HWHEEL, int(horizontal)))
        if events:
            self.emit(*events)


class Keyboard(Device):
    def __init__(self, path: str = UINPUT_PATH):
        super().__init__(KEYBOARD_NAME, keys=sorted(set(KEY.values()) | set(_ROW.values())), path=path)

    def tap(self, code: int, shift: bool = False, ctrl: bool = False) -> None:
        mods = ([KEY["Ctrl"]] if ctrl else []) + ([KEY["Shift"]] if shift else [])
        for mod in mods:
            self.emit((EV_KEY, mod, 1))
        self.emit((EV_KEY, code, 1))
        self.emit((EV_KEY, code, 0))
        for mod in reversed(mods):
            self.emit((EV_KEY, mod, 0))
