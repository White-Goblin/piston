"""Where the virtual pointer is, and how finger movement turns into pointer movement (pure, no I/O).

The real pointer moves in device pixels (1 uinput unit = 1 device pixel, no acceleration), so the position is tracked there
and divided by `scale` for drawing in the Steam interface (CSS pixels). Ported from the standalone Remote Control plugin."""
import math

DEFAULT_WIDTH, DEFAULT_HEIGHT = 1280, 800   # until the frontend reports the real screen
GAIN_PER_UNIT = 0.05                        # extra gain per CSS px of finger travel in one event
GAIN_CAP = 30                               # finger travel beyond this adds no more gain


class PointerTracker:
    def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT, scale: float = 1.0):
        self.scale = float(scale)
        self.width, self.height = int(width), int(height)      # device pixels
        self.x, self.y = self.width / 2, self.height / 2
        self._fx = self._fy = 0.0                                # sub-pixel carry of what was not injected yet
        self._sent = [0, 0]                                      # device pixels injected since the last take_sent()
        self.speed = 0.0                                         # smoothed finger speed in CSS px, for the cursor's trail

    def set_screen(self, width: int, height: int, scale: float) -> bool:
        """The screen in CSS px and its device pixel ratio. Nonsense values are ignored (False)."""
        if width < 100 or height < 100 or scale < 0.5:
            return False
        self.scale = float(scale)
        self.width, self.height = int(width * scale), int(height * scale)
        self.x = min(self.x, self.width - 1)
        self.y = min(self.y, self.height - 1)
        return True

    def park(self) -> None:
        """The pointer was pushed into the top-left corner (a known place): tracking starts from there."""
        self.x = self.y = 0.0
        self._fx = self._fy = 0.0
        self._sent = [0, 0]

    def move(self, dx: float, dy: float) -> tuple[int, int]:
        """Finger travel in CSS px -> the whole device pixels to inject now. The position moves by the exact amount; the
        fractional part is carried so what is injected over time matches it."""
        magnitude = math.hypot(dx, dy)
        gain = (1 + GAIN_PER_UNIT * min(magnitude, GAIN_CAP)) * self.scale
        self.x = min(max(self.x + dx * gain, 0), self.width - 1)
        self.y = min(max(self.y + dy * gain, 0), self.height - 1)
        self.speed = self.speed * 0.6 + magnitude * gain / self.scale * 0.4
        self._fx += dx * gain
        self._fy += dy * gain
        ix, iy = int(self._fx), int(self._fy)
        self._fx -= ix
        self._fy -= iy
        self._sent[0] += ix
        self._sent[1] += iy
        return ix, iy

    def clicked(self) -> None:
        self.speed = 0.0

    def snap_to(self, x: float, y: float) -> None:
        """The real pointer is somewhere else (another device moved it): follow it."""
        self.x = float(min(max(x, 0), self.width - 1))
        self.y = float(min(max(y, 0), self.height - 1))

    def take_sent(self) -> tuple[int, int]:
        """The device pixels injected since the last call (for the resync), reset to zero."""
        sent, self._sent = (self._sent[0], self._sent[1]), [0, 0]
        return sent

    def css_position(self) -> tuple[int, int]:
        return round(self.x / self.scale), round(self.y / self.scale)
