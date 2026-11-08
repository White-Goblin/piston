"""The feed behind the cursor overlay: the pointer position goes from the plugin to the transparent Steam view that draws it.

The view (an inline page in the Steam interface) long-polls `/cursor/events` on the plugin's loopback HTTP server (nonce
protected, see http_server.py). Each response holds every position since the page's last request, so a slow poll loses
nothing and a fast one gets small batches (measured on the device: about 2 ms behind at 60 positions per second).
Events to the Steam frontend (`cursor_activity`: input is flowing, `cursor_view`: hide or show the view around a click) go
through Decky's emit."""
import asyncio
import time

import decky

KEEP = 120               # positions kept for a page that is a moment late
POLL_TIMEOUT = 20.0      # a request with nothing to report is answered after this long (the page asks again)
ACTIVITY_REFRESH = 0.4   # the view stays alive 1.7 s after the last activity event: repeat the event well within that


class CursorFeed:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self._wall = wall
        self._items: list[dict] = []
        self._seq = 0
        self._waiters: set[asyncio.Future] = set()
        self._last_activity = float("-inf")
        self._last_down = False

    @property
    def seq(self) -> int:
        return self._seq

    # --- from the input handler (sync) --------------------------------------------------

    def publish(self, payload: dict) -> None:
        """A new pointer position {x, y, s, d} in CSS px (s: speed, d: a click just happened)."""
        self._add({**payload})
        down = bool(payload.get("d"))
        now = self._clock()
        if down != self._last_down or now - self._last_activity >= ACTIVITY_REFRESH:
            self._last_activity, self._last_down = now, down
            self._emit("cursor_activity", {"down": down})

    def badge(self, text: str) -> None:
        """A short label (the seconds a seek moved, +30) drawn beside the cursor for a moment. It carries the wall time (ms) it was
        made at, so a page that loads a moment later can tell how much of its time is left."""
        self._add({"badge": text, "ts": round(self._wall() * 1000)})

    def stop(self) -> None:
        """Input stopped (released, link lost): tell the page to hide the cursor now."""
        self._add({"stop": True})

    def abort(self) -> None:
        """Synchronous cleanup for unload: every waiting request is answered so none outlives the plugin."""
        self._wake()

    async def hide(self, hidden: bool) -> None:
        """Hide or show the view (it would swallow a click)."""
        await decky.emit("cursor_view", {"visible": not hidden})

    # --- from the HTTP route ------------------------------------------------------------

    async def poll(self, after: int, timeout: float = POLL_TIMEOUT) -> dict:
        """Everything newer than `after`, waiting up to `timeout` for something. A page that is ahead of the feed (the plugin
        restarted) is told to start over."""
        if after > self._seq:
            return {"seq": self._seq, "items": [], "reset": True}
        if self._seq == after:
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.add(waiter)
            try:
                await asyncio.wait_for(waiter, timeout)
            except asyncio.TimeoutError:
                pass
            finally:
                self._waiters.discard(waiter)
        return {"seq": self._seq, "items": [i for i in self._items if i["n"] > after]}

    # --- internals ----------------------------------------------------------------------

    def _add(self, item: dict) -> None:
        self._seq += 1
        item["n"] = self._seq
        self._items.append(item)
        del self._items[:-KEEP]
        self._wake()

    def _wake(self) -> None:
        for waiter in list(self._waiters):
            if not waiter.done():
                waiter.set_result(None)

    @staticmethod
    def _emit(event: str, data: dict) -> None:
        try:
            asyncio.ensure_future(decky.emit(event, data))
        except RuntimeError:  # no running loop (only possible in tests that call publish from outside one)
            pass
