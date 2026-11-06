"""Resync the tracked pointer with the real one.

Other devices (a controller trackpad, a mouse) or focus changes can move the real pointer, so dead
reckoning alone drifts. The X server of whichever window has focus follows the real pointer, while
the other server reports a stale value (a corner or the screen center). The live one is the display
whose pointer responds to our own moves."""
import asyncio
import ctypes
import ctypes.util
import glob
import logging
import time

log = logging.getLogger("decky-ha.pointer")


class XPointers:
    """Reads the pointer of every X display. Connections are opened per read: Xlib terminates the
    whole process if a held connection dies (an app's Xwayland going away), but a failed open is safe."""

    def __init__(self):
        name = ctypes.util.find_library("X11")
        if not name:
            raise OSError("libX11 not found")
        self.x11 = ctypes.CDLL(name)
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x11.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
        if not self.read():
            raise OSError("no X display reachable")

    def read(self):
        out = {}
        for sock in sorted(glob.glob("/tmp/.X11-unix/X*")):
            display = ":" + sock.rsplit("X", 1)[1]
            dpy = self.x11.XOpenDisplay(display.encode())
            if not dpy:
                continue
            try:
                root, child = ctypes.c_ulong(), ctypes.c_ulong()
                rx, ry, wx, wy, mask = (ctypes.c_int(), ctypes.c_int(), ctypes.c_int(),
                                        ctypes.c_int(), ctypes.c_uint())
                if self.x11.XQueryPointer(dpy, self.x11.XDefaultRootWindow(dpy), ctypes.byref(root),
                                          ctypes.byref(child), ctypes.byref(rx), ctypes.byref(ry),
                                          ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask)):
                    out[display] = (rx.value, ry.value)
            finally:
                self.x11.XCloseDisplay(dpy)
        return out


class Resync:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.prev = {}
        self.live = None
        self.silent = 0

    def begin(self, readings):
        """Baseline taken right before the first move of a burst."""
        self.prev = readings
        self.live = None
        self.silent = 0

    def _at_edge(self, x, y):
        return x <= 1 or y <= 1 or x >= self.width - 2 or y >= self.height - 2

    def update(self, readings, sent):
        """`sent` is the (dx, dy) in device pixels we injected since the last call. Returns the real
        pointer position when a live display is known, else None."""
        sx, sy = sent
        if abs(sx) + abs(sy) >= 2:
            best = None
            for d, (x, y) in readings.items():
                p = self.prev.get(d)
                if p is None:
                    continue
                dx, dy = x - p[0], y - p[1]
                if abs(dx) + abs(dy) < 2 or dx * sx < 0 or dy * sy < 0:
                    continue
                if abs(dx) > abs(sx) + 4 or abs(dy) > abs(sy) + 4:
                    continue
                err = abs(dx - sx) + abs(dy - sy)
                if best is None or err < best[0]:
                    best = (err, d)
            if best:
                self.live, self.silent = best[1], 0
            elif self.live and abs(sx) + abs(sy) >= 6 and not self._at_edge(*readings.get(self.live, (0, 0))):
                self.silent += 1
                if self.silent >= 3:  # the display stopped following us (focus moved elsewhere)
                    self.live = None
        self.prev = readings
        return readings.get(self.live) if self.live else None


SYNC_INTERVAL = 0.08   # seconds between real-pointer checks while input flows
SYNC_IDLE = 2.0        # stop checking this long after the last move
SYNC_SNAP = 20         # device px of disagreement before the tracked position is replaced
OPEN_RETRY = 30        # seconds between attempts to open the X displays when they were not up yet


class SyncLoop:
    """While pointer input flows, follow the real pointer (moved by other devices or by focus changes) so the drawn cursor
    matches it: every 80 ms compare what the X displays report with what was injected and snap the tracked position to the
    live display when they differ by more than 20 px. Runs only while input flows, and degrades to dead reckoning when
    the displays cannot be opened."""

    def __init__(self, tracker, on_snap=lambda: None, *, open_pointers=XPointers, clock=time.monotonic, sleep=asyncio.sleep,
                 interval: float = SYNC_INTERVAL, idle: float = SYNC_IDLE, snap: float = SYNC_SNAP, retry: float = OPEN_RETRY):
        self.tracker, self._on_snap = tracker, on_snap
        self._open, self._clock, self._sleep = open_pointers, clock, sleep
        self._interval, self._idle, self._snap, self._retry = interval, idle, snap, retry
        self._pointers = None
        self._tried = float("-inf")
        self._warned = False
        self._last_input = float("-inf")
        self._task: asyncio.Task | None = None
        self.resync: Resync | None = None

    @property
    def live_display(self) -> str | None:
        """The X display that follows the pointer (the one of the window with focus), when known."""
        return getattr(self.resync, "live", None)

    def note_move(self) -> None:
        """Called for every pointer move, BEFORE the move is injected: keeps the loop alive, and when it starts it takes its
        baseline reading of the displays (so the first move of a burst is seen)."""
        self._last_input = self._clock()
        if self._pointers is None and self._clock() - self._tried > self._retry:
            self._open_displays()
        if self._pointers is None or (self._task is not None and not self._task.done()):
            return
        self.resync = Resync(self.tracker.width, self.tracker.height)
        self.resync.begin(self._pointers.read())
        self.tracker.take_sent()
        self._task = asyncio.ensure_future(self._run())

    def _open_displays(self) -> None:
        self._tried = self._clock()
        try:
            self._pointers = self._open()
        except OSError as e:
            self._pointers = None
            if not self._warned:
                self._warned = True
                log.warning("pointer resync unavailable: %s", e)

    async def _run(self) -> None:
        try:
            while self._clock() - self._last_input < self._idle:
                await self._sleep(self._interval)
                real = self.resync.update(self._pointers.read(), self.tracker.take_sent())
                if real and (abs(real[0] - self.tracker.x) > self._snap or abs(real[1] - self.tracker.y) > self._snap):
                    self.tracker.snap_to(*real)
                    self._on_snap()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failing X read must not take the plugin down
            log.exception("pointer resync failed")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._pointers = None
        self.resync = None
