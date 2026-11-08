"""Remote input: the pointer, keyboard and Steam-interface actions that Home Assistant's remote card sends as `input` frames
(see docs/protocol.md).

Virtual devices are created on the first accepted event and closed when remote input is switched off, on unload and on
shutdown. Everything here that has to run at unload is synchronous (Decky starves the event loop after the unload request)."""
import asyncio
import concurrent.futures
import logging
import time

import decky

from . import uinput
from .cursor import CursorFeed
from .pointer import PointerTracker
from .pointer_sync import SyncLoop

log = logging.getLogger("decky-ha.input")

CREATE_RETRY = 30        # seconds between attempts to create the virtual devices after a failure
EMIT_INTERVAL = 1 / 60   # the cursor position is published at most this often
CLICK_DOWN, CLICK_UP_SETTLE = 0.03, 0.1
VIEW_ACK_TIMEOUT = 0.3
MAX_TEXT = 256
BUTTONS = {"l": uinput.BTN_LEFT, "r": uinput.BTN_RIGHT}
NUDGE_PX, NUDGE_BACK = 8.0, 0.08   # a seek nudges the pointer this far (CSS px) and moves it back this many seconds later


class InputMixin:
    def _init_input(self) -> None:
        settings = self._load_settings()
        self.remote_input = settings["remote_input"]
        self.seek_badge, self.seek_nudge = settings["seek_badge"], settings["seek_nudge"]
        self.input_nudged, self.input_nudge_timer = 0.0, None   # the way back of a nudge that is out, and its timer
        self.input_error = ""                      # why the virtual devices could not be created ("" = fine)
        self.input_pointer = None
        self.input_keyboard = None
        self.input_tracker = PointerTracker()
        self.input_sync = SyncLoop(self.input_tracker, on_snap=lambda: self._input_activity(force=True))
        self.input_keys = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="piston-keys")
        self.input_press_lock = asyncio.Lock()
        self.input_view_event = None
        self.input_last_emit = 0.0
        self.input_create_tried = float("-inf")
        self.cursor_feed = CursorFeed()            # the overlay's feed (cursor.py), served by the loopback HTTP route

    # --- devices ------------------------------------------------------------------------

    def _input_ready(self) -> bool:
        """The virtual devices exist (created now when needed). False, with the reason in `input_error`, when they cannot."""
        if self.input_pointer is not None and self.input_keyboard is not None:
            return True
        now = time.monotonic()
        if now - self.input_create_tried < CREATE_RETRY:
            return False
        self.input_create_tried = now
        pointer = keyboard = None
        try:
            pointer = uinput.Pointer()
            keyboard = uinput.Keyboard()
        except OSError as e:
            if pointer is not None:
                self._close_device(pointer)
            error = f"could not create the virtual input devices: {e}"
            if error != self.input_error:
                log.warning("remote input: %s", error)
                self.input_error = error
                self._push_input_group()
            return False
        self.input_pointer, self.input_keyboard = pointer, keyboard
        self.input_tracker.park()
        try:
            pointer.move(-20000, -20000)           # a known corner: tracking restarts there
        except OSError:
            pass
        if self.input_error:
            self.input_error = ""
            self._push_input_group()
        return True

    @staticmethod
    def _close_device(device) -> None:
        try:
            device.close()
        except OSError:
            pass

    def _close_input_devices(self) -> None:
        """Synchronous: safe to call while unloading."""
        self.input_sync.stop()
        for device in (self.input_pointer, self.input_keyboard):
            if device is not None:
                try:
                    device.release_all()
                except OSError:
                    pass
                self._close_device(device)
        self.input_pointer = self.input_keyboard = None

    def _input_release(self) -> None:
        """Let go of everything held and hide the cursor (link lost, the card went away, remote input switched off)."""
        self._input_unnudge()
        for device in (self.input_pointer, self.input_keyboard):
            if device is not None:
                try:
                    device.release_all()
                except OSError:
                    pass
        feed = self.cursor_feed
        if feed is not None:
            feed.stop()

    def _input_link_lost(self) -> None:
        self._input_release()

    def _push_input_group(self) -> None:
        push = getattr(self, "_push_device_group", None)
        if push is not None and getattr(self, "loop", None) is not None and self.loop.is_running():
            self.loop.create_task(push())

    def _input_field(self) -> dict:
        field = {"enabled": self.remote_input}
        if self.input_error and self.remote_input:
            field["error"] = self.input_error[:255]
        return field

    # --- panel callables ----------------------------------------------------------------

    async def set_remote_input(self, enabled: bool) -> dict:
        """Panel: allow or refuse remote input. Turning it off closes the virtual devices at once."""
        enabled = bool(enabled)
        self._update_settings({"remote_input": enabled})
        self.remote_input = enabled
        if not enabled:
            self._input_release()
            self._close_input_devices()
            self.input_error = ""
            self.input_create_tried = float("-inf")
        await self._push_device_group()
        return {"remote_input": enabled, "error": self.input_error}

    async def set_seek_badge(self, enabled: bool) -> dict:
        """Panel: draw how far a seek from the card moved beside the cursor (the circle flows aside and shows +30)."""
        self.seek_badge = bool(enabled)
        self._update_settings({"seek_badge": self.seek_badge})
        return {"seek_badge": self.seek_badge}

    async def set_seek_nudge(self, enabled: bool) -> dict:
        """Panel: nudge the pointer on a seek from the card, so the playing app shows its progress bar (it takes a seek for no input)."""
        self.seek_nudge = bool(enabled)
        self._update_settings({"seek_nudge": self.seek_nudge})
        if not self.seek_nudge:
            self._input_unnudge()
        return {"seek_nudge": self.seek_nudge}

    async def set_screen(self, width: int, height: int, scale: float) -> None:
        """The frontend reports the screen in CSS px and its pixel ratio (the real pointer moves in device pixels)."""
        self.input_tracker.set_screen(width, height, scale)

    async def get_cursor_config(self) -> dict:
        """The loopback server's port and nonce, for the cursor page the frontend builds."""
        return {"port": self.http_port, "nonce": self.nonce}

    async def view_ready(self) -> None:
        """The frontend confirms it hid or showed the cursor view."""
        if self.input_view_event is not None:
            self.input_view_event.set()

    # --- events from Home Assistant -----------------------------------------------------

    def _on_input(self, events: list) -> None:
        """Called by the protocol server for every accepted `input` frame (sync, must return quickly)."""
        if not self.remote_input:
            return
        for event in events:
            try:
                self._input_event(event)
            except OSError as e:                   # a device that went away mid-event
                log.warning("remote input event dropped: %r", e)
            except Exception:  # noqa: BLE001 - a bad event must not break the connection
                log.exception("remote input event failed")

    def _input_event(self, event: dict) -> None:
        kind = event["k"]
        if kind == "release":
            self._input_release()
            return
        if kind == "action":
            self._input_action(event["a"])
            return
        if not self._input_ready():
            return
        if kind == "move":
            self.input_sync.note_move()            # before the move: the resync takes its baseline
            ix, iy = self.input_tracker.move(event["dx"], event["dy"])
            self.input_pointer.move(ix, iy)
            self._input_activity()
        elif kind == "scroll":
            self.input_pointer.scroll(event["dy"], event["dx"])
            self._input_activity(force=True)
        elif kind == "click":
            self.input_tracker.clicked()
            self.loop.create_task(self._input_click(BUTTONS[event["b"]]))
        elif kind == "seek":
            self._input_seek(event["s"])
        elif kind == "key":
            self.input_keys.submit(self._input_tap, uinput.KEY[event["key"]])
        elif kind == "text":
            self.input_keys.submit(self._input_type, event["s"][:MAX_TEXT])

    def _input_seek(self, label: str) -> None:
        """The card seeked the playing media by `label` (+30). Show it beside the cursor and nudge the pointer, as the settings say.
        The nudge is not published to the cursor feed: the drawn cursor stays where it was and does not twitch."""
        if self.seek_badge:
            self._input_activity(force=True)         # the overlay must exist, and be where the pointer rests
            self.cursor_feed.badge(label)
        if self.seek_nudge:
            self._input_nudge()

    def _input_nudge(self) -> None:
        """Move the real pointer a few pixels (toward the middle of the screen) and, a moment later, back."""
        if self.input_pointer is None:
            return
        self._input_unnudge()
        tracker = self.input_tracker
        dx = -NUDGE_PX if tracker.x > tracker.width / 2 else NUDGE_PX
        self._input_nudge_move(dx)
        self.input_nudged = -dx
        try:
            self.input_nudge_timer = asyncio.get_running_loop().call_later(NUDGE_BACK, self._input_unnudge)
        except RuntimeError:                         # no loop (only a test calling from outside one): move back at once
            self._input_unnudge()

    def _input_unnudge(self) -> None:
        """Move the pointer back if a nudge is out. Synchronous: unload and release call it."""
        timer, self.input_nudge_timer = self.input_nudge_timer, None
        if timer is not None:
            timer.cancel()
        dx, self.input_nudged = self.input_nudged, 0.0
        if dx and self.input_pointer is not None:
            try:
                self._input_nudge_move(dx)
            except OSError as e:
                log.warning("nudge back failed: %r", e)

    def _input_nudge_move(self, dx: float) -> None:
        self.input_sync.note_move()                  # the resync takes its baseline before the move, as for any other move
        ix, iy = self.input_tracker.move(dx, 0)
        self.input_pointer.move(ix, iy)

    def _input_action(self, action: str) -> None:
        if getattr(self, "session", "game") == "desktop":
            return                                   # the Steam interface is not there; the card greys these tiles
        self.loop.create_task(decky.emit("remote_action", {"action": action}))

    # --- keyboard (one worker keeps the order) ------------------------------------------

    def _input_tap(self, code: int, **mods) -> None:
        keyboard = self.input_keyboard
        if keyboard is not None:
            try:
                keyboard.tap(code, **mods)
            except OSError as e:                   # the device was closed while this was queued
                log.warning("key dropped: %r", e)

    def _input_type(self, text: str) -> None:
        """ASCII only (non-ASCII has no key and is dropped, decision D15)."""
        for ch in text:
            mapped = uinput.char_to_key(ch)
            if mapped:
                self._input_tap(mapped[0], shift=mapped[1])

    # --- click, cursor ------------------------------------------------------------------

    async def _input_click(self, button: int) -> None:
        """The cursor overlay would swallow the click, so it is hidden around the press."""
        async with self.input_press_lock:
            pointer = self.input_pointer
            if pointer is None:
                return
            await self._input_hide_view(True)
            try:
                pointer.button(button, True)
                await asyncio.sleep(CLICK_DOWN)
            finally:
                try:
                    pointer.button(button, False)       # always: a cancelled or failed click must not leave the button down
                except OSError:
                    pass
            await asyncio.sleep(CLICK_UP_SETTLE)
            await self._input_hide_view(False)
            self._input_activity(down=True, force=True)

    async def _input_hide_view(self, hidden: bool) -> None:
        feed = self.cursor_feed
        if feed is None or getattr(self, "session", "game") == "desktop":
            return
        self.input_view_event = asyncio.Event()
        await feed.hide(hidden)
        if hidden:
            try:
                await asyncio.wait_for(self.input_view_event.wait(), VIEW_ACK_TIMEOUT)
            except asyncio.TimeoutError:
                pass

    def _input_activity(self, down: bool = False, force: bool = False) -> None:
        """Publish the cursor position (at most 60 times a second unless forced)."""
        feed = self.cursor_feed
        if feed is None or getattr(self, "session", "game") == "desktop":
            return
        now = time.monotonic()
        if not force and now - self.input_last_emit < EMIT_INTERVAL:
            return
        self.input_last_emit = now
        x, y = self.input_tracker.css_position()
        feed.publish({"x": x, "y": y, "s": round(self.input_tracker.speed, 1), "d": bool(down)})

    # --- unload -------------------------------------------------------------------------

    def _input_abort(self) -> None:
        """Synchronous cleanup for `_unload_now`: let go, close the devices, stop the worker."""
        self._input_release()
        self._close_input_devices()
        self.input_keys.shutdown(wait=False, cancel_futures=True)
        if self.cursor_feed is not None:
            self.cursor_feed.abort()                 # answer the waiting long-polls: none may outlive the plugin
