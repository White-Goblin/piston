import asyncio
import unittest

from piston_device import pointer_sync as ps
from piston_device.pointer import PointerTracker


class FakeDisplays:
    """Scripted X readings: `follow` is the display that moves with the pointer, `stale` one that does not."""

    def __init__(self, tracker=None):
        self.positions = {":0": (0, 0), ":1": (640, 400)}
        self.reads = 0

    def read(self):
        self.reads += 1
        return dict(self.positions)


class Harness:
    def __init__(self, displays=None, open_error=None, **kw):
        self.now = 0.0
        self.tracker = PointerTracker(1000, 800, 1.0)
        self.tracker.park()
        self.displays = displays or FakeDisplays()
        self.snaps = 0
        self.opens = 0
        self.open_error = open_error

        def open_pointers():
            self.opens += 1
            if self.open_error:
                raise OSError(self.open_error)
            return self.displays

        async def sleep(seconds):
            self.now += seconds
            await asyncio.sleep(0)

        self.loop = ps.SyncLoop(self.tracker, lambda: setattr(self, "snaps", self.snaps + 1), open_pointers=open_pointers,
                                clock=lambda: self.now, sleep=sleep, **kw)

    def move(self, dx, dy):
        """The pointer is moved by us: the loop is told first (it takes its baseline), then the tracker and the live display move."""
        self.loop.note_move()
        ix, iy = self.tracker.move(dx, dy)
        x, y = self.displays.positions[":0"]
        self.displays.positions[":0"] = (x + ix, y + iy)

    async def settle(self, n=5):
        for _ in range(n):
            await asyncio.sleep(0)


class ResyncTests(unittest.TestCase):
    def test_the_display_that_follows_our_moves_becomes_live(self):
        r = ps.Resync(1000, 800)
        r.begin({":0": (100, 100), ":1": (640, 400)})
        real = r.update({":0": (110, 105), ":1": (640, 400)}, (10, 5))
        self.assertEqual((r.live, real), (":0", (110, 105)))

    def test_a_display_moving_the_wrong_way_is_not_live(self):
        r = ps.Resync(1000, 800)
        r.begin({":0": (100, 100)})
        self.assertIsNone(r.update({":0": (90, 100)}, (10, 0)))

    def test_a_display_that_stops_following_loses_live_after_three_silent_ticks(self):
        r = ps.Resync(1000, 800)
        r.begin({":0": (100, 100)})
        r.update({":0": (110, 100)}, (10, 0))
        for _ in range(3):
            r.update({":0": (110, 100)}, (10, 0))
        self.assertIsNone(r.live)


class SyncLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_real_pointer_that_disagrees_by_more_than_20_px_causes_one_snap(self):
        h = Harness()
        h.move(10, 0)
        await h.settle()
        # another device moves the real pointer a long way, in a direction we also moved
        h.displays.positions[":0"] = (h.displays.positions[":0"][0] + 12, 200)
        h.tracker.move(10, 0)
        await h.settle()
        self.assertEqual(h.snaps, 1)
        self.assertEqual(h.tracker.y, 200.0)
        h.loop.stop()

    async def test_a_disagreement_of_20_px_or_less_does_not_snap(self):
        h = Harness()
        h.move(10, 0)
        await h.settle()
        x, y = h.displays.positions[":0"]
        h.displays.positions[":0"] = (x + 11, y + 10)
        h.tracker.move(10, 0)
        await h.settle()
        self.assertEqual(h.snaps, 0)
        h.loop.stop()

    async def test_the_loop_stops_two_seconds_after_the_last_move_and_restarts_on_the_next(self):
        h = Harness()
        h.move(10, 0)
        await h.settle(5)
        self.assertFalse(h.loop._task.done())
        for _ in range(40):                     # 40 * 0.08 s = 3.2 s with no moves
            await h.settle(1)
        self.assertTrue(h.loop._task.done())
        reads = h.displays.reads
        h.move(10, 0)
        await h.settle(5)
        self.assertGreater(h.displays.reads, reads)
        self.assertFalse(h.loop._task.done())
        h.loop.stop()

    async def test_only_one_loop_runs_while_moves_keep_coming(self):
        h = Harness()
        h.move(5, 0)
        first = h.loop._task
        h.move(5, 0)
        h.move(5, 0)
        self.assertIs(h.loop._task, first)
        h.loop.stop()

    async def test_unreachable_displays_degrade_to_dead_reckoning_with_one_warning_and_a_retry_later(self):
        h = Harness(open_error="no X display reachable")
        with self.assertLogs("decky-ha.pointer", "WARNING") as logs:
            h.loop.note_move()
            h.now += 10
            h.loop.note_move()                  # inside the 30 s retry window: no new attempt
            self.assertEqual(h.opens, 1)
            h.now += 25
            h.loop.note_move()                  # past it: tries again, no second warning
        self.assertEqual((h.opens, len(logs.records)), (2, 1))
        self.assertIsNone(h.loop._task)
        self.assertEqual(h.snaps, 0)

    async def test_displays_that_come_up_later_are_picked_up(self):
        h = Harness(open_error="not yet")
        with self.assertLogs("decky-ha.pointer", "WARNING"):
            h.loop.note_move()
        h.open_error = None
        h.now += 31
        h.loop.note_move()
        self.assertIsNotNone(h.loop._task)
        h.loop.stop()

    async def test_a_failing_read_is_logged_and_does_not_escape(self):
        h = Harness()
        h.move(5, 0)
        h.displays.read = lambda: (_ for _ in ()).throw(RuntimeError("x gone"))
        with self.assertLogs("decky-ha.pointer", "ERROR"):
            await h.settle(5)
        self.assertTrue(h.loop._task.done())

    async def test_stop_cancels_the_task_and_forgets_the_displays(self):
        h = Harness()
        h.move(5, 0)
        task = h.loop._task
        h.loop.stop()
        await h.settle()
        self.assertTrue(task.cancelled() or task.done())
        self.assertIsNone(h.loop._pointers)
        self.assertIsNone(h.loop.live_display)

    async def test_the_live_display_is_exposed(self):
        h = Harness()
        h.move(10, 0)
        await h.settle()
        self.assertEqual(h.loop.live_display, ":0")
        h.loop.stop()
