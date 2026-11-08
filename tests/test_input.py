import asyncio
import time
import types
import unittest
from unittest import mock

import tests
from . import use_temp_settings_dir
from piston_device import input as input_module
from piston_device import uinput
from piston_device.input import InputMixin
from piston_device.settings import SettingsMixin
from piston_device.state import StateMixin


class FakePointer:
    def __init__(self, log):
        self.log, self.closed = log, False

    def move(self, dx, dy):
        self.log.append(("move", dx, dy))

    def button(self, button, down):
        self.log.append(("button", button, down))

    def scroll(self, vertical, horizontal=0):
        self.log.append(("scroll", vertical, horizontal))

    def release_all(self):
        self.log.append(("release_all", "pointer"))

    def close(self):
        self.closed = True
        self.log.append(("close", "pointer"))


class FakeKeyboard:
    def __init__(self, log):
        self.log, self.closed = log, False

    def tap(self, code, shift=False, ctrl=False):
        self.log.append(("tap", code, shift))

    def release_all(self):
        self.log.append(("release_all", "keyboard"))

    def close(self):
        self.closed = True
        self.log.append(("close", "keyboard"))


class FakeFeed:
    def __init__(self, log):
        self.log, self.published = log, []
        self.view_event = None

    def publish(self, payload):
        self.published.append(payload)

    async def hide(self, hidden):
        self.log.append(("hide", hidden))
        if self.view_event is not None:
            self.view_event()

    def badge(self, text):
        self.log.append(("badge", text))

    def stop(self):
        self.log.append(("feed_stop",))

    def abort(self):
        self.log.append(("feed_abort",))


class FakeServer:
    def __init__(self):
        self.states = []

    def push_state(self, name, data):
        self.states.append((name, data))


class Host(SettingsMixin, StateMixin, InputMixin):
    def __init__(self):
        self._init_state()
        self._init_input()
        self.session = "game"


class InputTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        use_temp_settings_dir()
        tests.fake.emitted.clear()
        self.log = []
        self.made = []

        def make_pointer():
            p = FakePointer(self.log)
            self.made.append(p)
            return p

        def make_keyboard():
            k = FakeKeyboard(self.log)
            self.made.append(k)
            return k

        for name, factory in (("Pointer", make_pointer), ("Keyboard", make_keyboard)):
            patcher = mock.patch.object(input_module.uinput, name, factory)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.h = Host()
        self.h.loop = asyncio.get_running_loop()
        self.h.server = FakeServer()
        self.feed = FakeFeed(self.log)
        self.h.cursor_feed = self.feed
        self.addCleanup(self.h.input_keys.shutdown, False)
        self.addCleanup(self.h.input_sync.stop)

    def ready(self):
        """The virtual devices exist (first use parks the pointer) and the log and the feed start empty."""
        self.h.input_tracker.set_screen(1000, 800, 1.0)
        self.assertTrue(self.h._input_ready())
        self.log.clear()
        self.feed.published.clear()

    async def settle(self, seconds=0.0):
        await asyncio.sleep(seconds)

    def calls(self, kind):
        return [c for c in self.log if c[0] == kind]

    async def wait_for(self, predicate, timeout=2.0):
        end = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), end, "timed out")
            await asyncio.sleep(0.005)


class DeviceLifecycleTests(InputTestCase):
    async def test_nothing_is_created_until_the_first_event(self):
        self.assertEqual(self.made, [])
        self.h._on_input([{"k": "move", "dx": 4, "dy": 0}])
        self.assertEqual(len(self.made), 2)
        self.assertEqual(self.log[0], ("move", -20000, -20000))          # parked in a known corner first
        self.assertGreater(self.h.input_tracker.x, 0)                    # tracking restarted from the corner

    async def test_devices_are_created_once(self):
        self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
        self.h._on_input([{"k": "move", "dx": 1, "dy": 0}, {"k": "key", "key": "Enter"}])
        self.assertEqual(len(self.made), 2)

    async def test_remote_input_off_creates_nothing_and_ignores_frames(self):
        await self.h.set_remote_input(False)
        self.h._on_input([{"k": "move", "dx": 4, "dy": 0}, {"k": "click", "b": "l"}, {"k": "key", "key": "Enter"}])
        await self.settle(0.05)
        self.assertEqual(self.made, [])
        self.assertEqual([c for c in self.log if c[0] in ("move", "button", "tap", "hide")], [])

    async def test_switching_off_closes_the_devices_at_once_and_updates_the_device_group(self):
        self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
        pushed = len(self.h.server.states)
        result = await self.h.set_remote_input(False)
        self.assertEqual(result["remote_input"], False)
        self.assertTrue(all(d.closed for d in self.made))
        self.assertIsNone(self.h.input_pointer)
        self.assertEqual(self.h._load_settings()["remote_input"], False)
        name, group = self.h.server.states[-1]
        self.assertEqual((name, group["input"]), ("device", {"enabled": False}))
        self.assertGreater(len(self.h.server.states), pushed)
        await self.h.set_remote_input(True)
        self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])               # back on: devices are created again
        self.assertEqual(len(self.made), 4)

    async def test_the_setting_defaults_to_on_and_survives_a_bad_stored_value(self):
        self.assertTrue(self.h.remote_input)
        self.h._update_settings({"remote_input": "yes"})
        self.assertIs(self.h._load_settings()["remote_input"], True)

    async def test_unload_cleanup_is_synchronous_and_complete(self):
        self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
        self.h._input_abort()
        self.assertTrue(all(d.closed for d in self.made))
        self.assertIn(("feed_stop",), self.log)
        self.assertIn(("feed_abort",), self.log)                           # waiting long-polls are answered
        self.assertEqual(self.calls("release_all"), [("release_all", "pointer"), ("release_all", "keyboard")] * 2)
        with self.assertRaises(RuntimeError):
            self.h.input_keys.submit(lambda: None)                         # the worker is shut down

    async def test_creation_failure_is_reported_once_dropped_and_retried_only_after_30_seconds(self):
        attempts = []

        def failing_pointer():
            attempts.append(1)
            raise PermissionError("/dev/uinput: permission denied")

        clock = [1000.0]
        with mock.patch.object(input_module.uinput, "Pointer", failing_pointer), \
                mock.patch.object(input_module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])), \
                self.assertLogs("decky-ha.input", "WARNING") as logs:
            self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
            await self.settle(0.01)
            clock[0] += 10
            self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
            self.assertEqual(len(attempts), 1)
            clock[0] += 25
            self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
            await self.settle(0.01)
        self.assertEqual((len(attempts), len(logs.records)), (2, 1))       # one clear line, not one per frame
        group = [d for n, d in self.h.server.states if n == "device"][-1]
        self.assertTrue(group["input"]["enabled"])
        self.assertIn("permission denied", group["input"]["error"])
        self.assertEqual(self.calls("move"), [])
        # once it works the error goes away
        clock[0] += 31
        with mock.patch.object(input_module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])):
            self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
        await self.settle(0.01)
        self.assertEqual(self.h.input_error, "")
        self.assertNotIn("error", [d for n, d in self.h.server.states if n == "device"][-1]["input"])

    async def test_a_keyboard_failure_closes_the_pointer_again(self):
        def failing_keyboard():
            raise OSError("no")
        with mock.patch.object(input_module.uinput, "Keyboard", failing_keyboard), self.assertLogs("decky-ha.input", "WARNING"):
            self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
        self.assertTrue(self.made[0].closed)
        self.assertIsNone(self.h.input_pointer)


class EventTests(InputTestCase):
    async def test_moves_scrolls_and_the_cursor_feed(self):
        self.h.input_tracker.set_screen(1000, 800, 1.0)
        self.h._on_input([{"k": "move", "dx": 10, "dy": 0}, {"k": "scroll", "dx": 1, "dy": -2}])
        self.assertIn(("move", 15, 0), self.log)
        self.assertIn(("scroll", -2, 1), self.log)                         # vertical first, then horizontal
        self.assertEqual(self.feed.published[-1]["d"], False)
        self.assertEqual(len(self.feed.published), 2)

    async def test_a_seek_shows_the_badge_and_nudges_the_pointer_out_and_back_without_moving_the_drawn_cursor(self):
        self.ready()
        self.h._on_input([{"k": "seek", "s": "+30"}])
        self.assertIn(("badge", "+30"), self.log)
        self.assertEqual(len(self.feed.published), 1)                       # the resting position, so the overlay exists; the nudge is not published
        moves = self.calls("move")
        self.assertEqual(len(moves), 1)
        self.assertGreater(moves[0][1], 0)                                  # left half of the screen: out to the right
        await self.settle(input_module.NUDGE_BACK + 0.05)
        moves = self.calls("move")
        self.assertEqual(sum(m[1] for m in moves), 0)                       # and back
        self.assertEqual(len(self.feed.published), 1)
        self.assertEqual(self.h.input_nudged, 0.0)

    async def test_the_nudge_goes_toward_the_middle_of_the_screen(self):
        self.ready()
        self.h.input_tracker.snap_to(900, 400)
        self.h._on_input([{"k": "seek", "s": "-10"}])
        self.assertLess(self.calls("move")[0][1], 0)
        self.h._input_unnudge()

    async def test_each_setting_turns_its_part_off(self):
        self.ready()
        self.h.seek_badge = False
        self.h._on_input([{"k": "seek", "s": "+10"}])
        self.assertEqual((self.calls("badge"), len(self.feed.published), len(self.calls("move"))), ([], 0, 1))
        self.h._input_unnudge()
        self.log.clear()
        self.h.seek_badge, self.h.seek_nudge = True, False
        self.h._on_input([{"k": "seek", "s": "+10"}])
        self.assertEqual((len(self.calls("badge")), self.calls("move")), (1, []))

    async def test_a_nudge_that_is_out_is_taken_back_by_release_and_by_unload(self):
        self.ready()
        for finish in (self.h._input_release, self.h._input_abort):
            self.log.clear()
            self.h._on_input([{"k": "seek", "s": "+10"}])
            finish()
            self.assertEqual(sum(m[1] for m in self.calls("move")), 0)
            self.assertEqual(self.h.input_nudged, 0.0)
            self.assertIsNone(self.h.input_nudge_timer)

    async def test_without_remote_input_a_seek_does_nothing(self):
        self.h.remote_input = False
        self.h._on_input([{"k": "seek", "s": "+30"}])
        self.assertEqual((self.log, self.feed.published), ([], []))

    async def test_the_cursor_position_is_published_at_most_sixty_times_a_second(self):
        self.h._on_input([{"k": "move", "dx": 1, "dy": 0}] * 100)
        self.assertLess(len(self.feed.published), 5)

    async def test_a_click_hides_the_view_presses_releases_and_shows_it_again_in_order(self):
        with mock.patch.object(input_module, "CLICK_UP_SETTLE", 0.0):
            self.h._on_input([{"k": "click", "b": "r"}])
            await self.wait_for(lambda: ("hide", False) in self.log)
        events = [c for c in self.log if c[0] in ("hide", "button")]
        self.assertEqual(events, [("hide", True), ("button", uinput.BTN_RIGHT, True), ("button", uinput.BTN_RIGHT, False), ("hide", False)])
        self.assertTrue(self.feed.published[-1]["d"])

    async def test_a_cancelled_click_still_lets_go_of_the_button(self):
        gate = asyncio.Event()

        async def slow_hide(hidden):
            if not hidden:
                await gate.wait()
        self.feed.hide = slow_hide
        self.h._on_input([{"k": "click", "b": "l"}])
        await self.wait_for(lambda: ("button", uinput.BTN_LEFT, False) in self.log)
        task = next(t for t in asyncio.all_tasks() if "_input_click" in repr(t))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        downs = [c for c in self.log if c[0] == "button"]
        self.assertEqual(downs[-1], ("button", uinput.BTN_LEFT, False))

    async def test_keys_and_text_are_typed_in_order_and_non_ascii_is_dropped(self):
        self.h._on_input([{"k": "text", "s": "Hi é!"}, {"k": "key", "key": "Enter"}, {"k": "key", "key": "Backspace"}])
        await self.wait_for(lambda: len(self.calls("tap")) == 6)
        self.assertEqual(self.calls("tap"), [
            ("tap", 35, True), ("tap", 23, False), ("tap", 57, False),      # H (shift), i, space
            ("tap", 2, True),                                              # ! (shift 1); the é in between has no key
            ("tap", uinput.KEY["Enter"], False), ("tap", uinput.KEY["Backspace"], False)])

    async def test_long_text_is_cut_at_256_characters(self):
        self.h._on_input([{"k": "text", "s": "a" * 300}])
        await self.wait_for(lambda: len(self.calls("tap")) >= 256)
        await self.settle(0.05)
        self.assertEqual(len(self.calls("tap")), 256)

    async def test_release_lets_go_of_everything_and_hides_the_cursor(self):
        self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
        self.log.clear()
        self.h._on_input([{"k": "release"}])
        self.assertEqual(self.log, [("release_all", "pointer"), ("release_all", "keyboard"), ("feed_stop",)])

    async def test_a_lost_link_releases_everything(self):
        self.h._on_input([{"k": "move", "dx": 1, "dy": 0}])
        self.log.clear()
        self.h._input_link_lost()
        self.assertEqual(self.log, [("release_all", "pointer"), ("release_all", "keyboard"), ("feed_stop",)])

    async def test_release_before_any_event_is_harmless(self):
        self.h._on_input([{"k": "release"}])
        self.assertEqual(self.made, [])

    async def test_steam_and_quick_access_actions_go_to_the_frontend(self):
        self.h._on_input([{"k": "action", "a": "steam"}, {"k": "action", "a": "qam"}])
        await self.settle(0.02)
        self.assertEqual(tests.fake.emitted, [("remote_action", ({"action": "steam"},)), ("remote_action", ({"action": "qam"},))])
        self.assertEqual(self.made, [])                                    # actions need no virtual device

    async def test_desktop_mode_keeps_the_pointer_but_has_no_cursor_and_no_steam_actions(self):
        self.h.session = "desktop"
        self.h._on_input([{"k": "move", "dx": 10, "dy": 0}, {"k": "scroll", "dx": 0, "dy": 1},
                          {"k": "action", "a": "steam"}, {"k": "click", "b": "l"}])
        await self.settle(0.3)
        self.assertEqual(self.feed.published, [])
        self.assertEqual(tests.fake.emitted, [])
        self.assertNotIn(("hide", True), self.log)
        self.assertIn(("button", uinput.BTN_LEFT, True), self.log)         # the click itself still happens
        self.assertIn(("move", 15, 0), self.log)

    async def test_the_view_ready_callable_releases_the_wait(self):
        waited = []

        async def hide(hidden):
            self.log.append(("hide", hidden))

        self.feed.hide = hide
        with mock.patch.object(input_module, "CLICK_UP_SETTLE", 0.0):
            self.h._on_input([{"k": "click", "b": "l"}])
            await self.wait_for(lambda: self.h.input_view_event is not None)
            started = time.monotonic()
            await self.h.view_ready()
            await self.wait_for(lambda: ("hide", False) in self.log)
        self.assertLess(time.monotonic() - started, input_module.VIEW_ACK_TIMEOUT)

    async def test_a_bad_event_does_not_break_the_rest(self):
        with self.assertLogs("decky-ha.input", "ERROR"):
            self.h._on_input([{"k": "key", "key": "NoSuchKey"}, {"k": "scroll", "dx": 0, "dy": 3}])
        self.assertIn(("scroll", 3, 0), self.log)


class ReportedStateTests(InputTestCase):
    async def test_the_device_group_carries_the_input_field(self):
        group = await self.h._device_group()
        self.assertEqual(group["input"], {"enabled": True})

    async def test_set_screen_feeds_the_tracker(self):
        await self.h.set_screen(1377, 775, 2.79)
        self.assertEqual(self.h.input_tracker.width, 3841)
