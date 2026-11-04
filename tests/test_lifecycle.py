import asyncio
import os
import tempfile
import unittest
from unittest import mock

from piston_device import lifecycle
from piston_device.lifecycle import Lifecycle


class FakeStdin:
    def __init__(self, proc):
        self.proc = proc

    def close(self):
        self.proc.end(0)


class FakeProc:
    """Stands in for both `gdbus monitor` (feed lines) and `systemd-inhibit ... cat` (stdin close ends it)."""

    def __init__(self, argv):
        self.argv = argv
        self.stdout = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self.returncode = None
        self.killed = False
        self._done = asyncio.Event()

    def feed(self, signal, value):
        self.stdout.feed_data(f"/org/freedesktop/login1: org.freedesktop.login1.Manager.{signal} ({value},)\n".encode())

    def end(self, code=0):
        if self.returncode is None:
            self.returncode = code
            self.stdout.feed_eof()
            self._done.set()

    def kill(self):
        self.killed = True
        self.end(-9)

    async def wait(self):
        await self._done.wait()
        return self.returncode


class Harness:
    def __init__(self, missing=(), **kw):
        self.monitors, self.inhibitors, self.events, self.sleeps = [], [], [], []
        self.missing = missing
        self.block = None     # an Event the sleep callback waits for (to test the cap)

        async def spawn(*argv, **_):
            if argv[0] in self.missing:
                raise FileNotFoundError(argv[0])
            proc = FakeProc(argv)
            (self.inhibitors if argv[0] == "systemd-inhibit" else self.monitors).append(proc)
            return proc

        async def on_sleep():
            self.events.append("sleep")
            if self.block is not None:
                await self.block.wait()

        async def on_shutdown(reason):
            self.events.append(("shutdown", reason))

        async def on_resume():
            self.events.append("resume")

        async def fake_sleep(seconds):
            self.sleeps.append(seconds)
            await asyncio.sleep(0)

        async def reason():
            return "restart" if getattr(self, "restart", False) else "shutdown"

        self.lc = Lifecycle(on_sleep=on_sleep, on_shutdown=on_shutdown, on_resume=on_resume, spawn=spawn,
                            sleep=fake_sleep, reason=reason, reap=lambda: 0, **kw)  # never reap the machine the tests run on

    @property
    def held(self):
        return [p for p in self.inhibitors if p.returncode is None]

    async def settle(self, tries=50):
        for _ in range(tries):
            await asyncio.sleep(0.005)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.addAsyncCleanup(self.h.lc.stop)

    async def started(self):
        await self.h.lc.start()
        await self.h.settle(5)
        return self.h.monitors[0]

    async def test_start_takes_the_inhibitor_and_the_monitor_and_is_active(self):
        await self.started()
        self.assertTrue(self.h.lc.active)
        self.assertEqual(len(self.h.held), 1)
        self.assertIn("--mode=delay", self.h.held[0].argv)
        self.assertEqual(self.h.monitors[0].argv[:3], ("gdbus", "monitor", "--system"))

    async def test_sleep_signal_announces_then_releases_the_inhibitor(self):
        mon = await self.started()
        mon.feed("PrepareForSleep", "true")
        await self.h.settle()
        self.assertEqual(self.h.events, ["sleep"])
        self.assertEqual(self.h.held, [])             # released as soon as on_sleep returned

    async def test_a_slow_announcement_is_cut_off_at_the_cap(self):
        self.h = Harness(inhibit_cap=0.05)
        self.addAsyncCleanup(self.h.lc.stop)
        self.h.block = asyncio.Event()
        mon = await self.started()
        mon.feed("PrepareForSleep", "true")
        await self.h.settle(40)
        self.assertEqual(self.h.held, [])             # never held the suspend beyond the cap

    async def test_a_failing_announcement_still_releases_the_inhibitor(self):
        mon = await self.started()

        async def boom():
            raise RuntimeError("push failed")

        self.h.lc._on_sleep = boom
        mon.feed("PrepareForSleep", "true")
        await self.h.settle()
        self.assertEqual(self.h.held, [])
        self.assertTrue(self.h.lc.active)             # and the monitor keeps running

    async def test_resume_signal_calls_back_and_retakes_the_inhibitor(self):
        mon = await self.started()
        mon.feed("PrepareForSleep", "true")
        await self.h.settle()
        mon.feed("PrepareForSleep", "false")
        await self.h.settle()
        self.assertEqual(self.h.events, ["sleep", "resume"])
        self.assertEqual(len(self.h.held), 1)
        self.assertEqual(len(self.h.inhibitors), 2)

    async def test_a_slow_resume_does_not_block_the_next_signal_and_is_cancelled_by_a_sleep(self):
        mon = await self.started()
        started, cancelled = asyncio.Event(), []

        async def slow_resume():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        self.h.lc._on_resume = slow_resume
        mon.feed("PrepareForSleep", "false")
        await asyncio.wait_for(started.wait(), 1)
        self.assertEqual(len(self.h.held), 1)         # re-taken before the slow refresh, not after
        mon.feed("PrepareForSleep", "true")           # the device goes down again while it was refreshing
        await self.h.settle()
        self.assertEqual(self.h.events, ["sleep"])
        self.assertEqual(cancelled, [True])

    async def test_protecting_means_monitor_running_and_inhibitor_held(self):
        self.assertFalse(self.h.lc.protecting)
        mon = await self.started()
        self.assertTrue(self.h.lc.protecting)
        mon.feed("PrepareForSleep", "true")
        await self.h.settle()
        self.assertFalse(self.h.lc.protecting)        # released for the suspend
        mon.feed("PrepareForSleep", "false")
        await self.h.settle()
        self.assertTrue(self.h.lc.protecting)

    async def test_without_the_inhibit_tool_it_is_not_protecting(self):
        self.h = Harness(missing=("systemd-inhibit",))
        self.addAsyncCleanup(self.h.lc.stop)
        await self.h.lc.start()
        await self.h.settle(5)
        self.assertTrue(self.h.lc.active)
        self.assertFalse(self.h.lc.protecting)

    async def test_resume_retakes_the_inhibitor_even_if_the_callback_fails(self):
        mon = await self.started()

        async def boom():
            raise RuntimeError("refresh failed")

        self.h.lc._on_resume = boom
        mon.feed("PrepareForSleep", "true")
        await self.h.settle()
        mon.feed("PrepareForSleep", "false")
        await self.h.settle()
        self.assertEqual(len(self.h.held), 1)

    async def test_shutdown_signal_gives_the_reason(self):
        mon = await self.started()
        mon.feed("PrepareForShutdown", "true")
        await self.h.settle()
        self.assertEqual(self.h.events, [("shutdown", "shutdown")])
        self.h.restart = True
        mon.feed("PrepareForShutdown", "true")
        await self.h.settle()
        self.assertEqual(self.h.events[-1], ("shutdown", "restart"))

    async def test_a_cancelled_shutdown_says_nothing(self):
        mon = await self.started()
        mon.feed("PrepareForShutdown", "false")
        await self.h.settle()
        self.assertEqual(self.h.events, [])

    async def test_other_lines_are_ignored(self):
        mon = await self.started()
        mon.stdout.feed_data(b"/org/freedesktop/login1: org.freedesktop.login1.Manager.SessionNew ('1', objectpath '/x')\n")
        mon.stdout.feed_data(b"garbage\n")
        await self.h.settle()
        self.assertEqual(self.h.events, [])
        self.assertTrue(self.h.lc.active)

    async def test_the_monitor_is_restarted_when_it_exits_and_inactive_meanwhile(self):
        mon = await self.started()
        mon.end(1)
        await self.h.settle(5)
        self.assertEqual(self.h.sleeps[:1], [1])
        self.assertEqual(len(self.h.monitors), 2)
        self.assertTrue(self.h.lc.active)
        self.h.monitors[1].feed("PrepareForSleep", "true")
        await self.h.settle()
        self.assertEqual(self.h.events, ["sleep"])

    async def test_restarts_are_bounded_then_it_stays_inactive(self):
        await self.started()
        for i in range(len(lifecycle.RESTART_DELAYS) + 1):
            await self.h.settle(5)
            self.h.monitors[-1].end(1)
        await self.h.settle(10)
        self.assertEqual(self.h.sleeps, list(lifecycle.RESTART_DELAYS))
        self.assertEqual(len(self.h.monitors), len(lifecycle.RESTART_DELAYS) + 1)
        self.assertFalse(self.h.lc.active)

    async def test_a_stable_monitor_resets_the_retry_count(self):
        now = [0.0]
        self.h = Harness(clock=lambda: now[0])
        self.addAsyncCleanup(self.h.lc.stop)
        await self.started()
        for _ in range(3):
            now[0] += lifecycle.STABLE_SECONDS + 1
            self.h.monitors[-1].end(1)
            await self.h.settle(5)
        self.assertEqual(self.h.sleeps, [1, 1, 1])

    async def test_no_inhibitor_is_held_while_the_monitor_is_down(self):
        mon = await self.started()
        self.assertEqual(len(self.h.held), 1)
        self.h.sleeps.clear()
        gate = asyncio.Event()

        async def slow_sleep(seconds):
            self.h.sleeps.append(seconds)
            await gate.wait()

        self.h.lc._sleep = slow_sleep
        mon.end(1)
        await self.h.settle(5)
        self.assertEqual(self.h.held, [])             # waiting to restart: nothing may delay a suspend
        gate.set()
        await self.h.settle(5)
        self.assertEqual(len(self.h.held), 1)         # monitor back: held again

    async def test_missing_gdbus_does_not_raise_and_stays_inactive(self):
        self.h = Harness(missing=("gdbus",))
        self.addAsyncCleanup(self.h.lc.stop)
        await self.h.lc.start()
        await self.h.settle(5)
        self.assertFalse(self.h.lc.active)
        self.assertEqual(self.h.monitors, [])
        self.assertEqual(self.h.sleeps, [])           # no pointless retries
        self.assertEqual(self.h.held, [])             # and no inhibitor nobody would release

    async def test_missing_inhibit_tool_still_announces(self):
        self.h = Harness(missing=("systemd-inhibit",))
        self.addAsyncCleanup(self.h.lc.stop)
        await self.h.lc.start()
        await self.h.settle(5)
        self.assertTrue(self.h.lc.active)
        self.h.monitors[0].feed("PrepareForSleep", "true")
        await self.h.settle()
        self.assertEqual(self.h.events, ["sleep"])

    async def test_stop_kills_the_monitor_and_releases_the_inhibitor(self):
        await self.started()
        await self.h.lc.stop()
        self.assertFalse(self.h.lc.active)
        self.assertTrue(self.h.monitors[0].killed)
        self.assertEqual(self.h.held, [])


if __name__ == "__main__":
    unittest.main()


class AbortTests(unittest.IsolatedAsyncioTestCase):
    async def test_abort_kills_the_monitor_and_the_inhibitor_at_once_without_awaiting(self):
        h = Harness()
        lc = h.lc
        await lc.start()
        for _ in range(50):
            await asyncio.sleep(0)
            if h.monitors and h.inhibitors:
                break
        monitor, inhibitor = h.monitors[0], h.inhibitors[0]
        lc.abort()                                   # a plain call: no await, as in a starved event loop
        self.assertTrue(monitor.killed and inhibitor.killed)
        self.assertFalse(lc.active)
        lc.abort()                                   # twice is fine
        await lc.stop()

    async def test_start_reaps_the_orphans_of_an_earlier_run_once(self):
        reaped = []
        h = Harness()
        h.lc._reap = lambda: reaped.append(1) or 0
        await h.lc.start()
        await h.lc.stop()
        self.assertEqual(reaped, [1])

    async def test_abort_before_start_is_harmless(self):
        Harness().lc.abort()


class ReapOrphansTests(unittest.TestCase):
    def make(self, root, pid, argv, ppid, uid=None):
        d = os.path.join(root, str(pid))
        os.mkdir(d)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        with open(os.path.join(d, "stat"), "w") as f:
            f.write(f"{pid} (gdbus) S {ppid} 1 1 0 -1\n")

    def test_only_our_orphaned_monitors_are_stopped(self):
        monitor = list(lifecycle.MONITOR_ARGV)
        with tempfile.TemporaryDirectory() as root:
            self.make(root, 101, monitor, 1)                                   # orphaned: reaped
            self.make(root, 102, monitor, 4242)                                # has a living parent (a running plugin): kept
            self.make(root, 103, ["gdbus", "monitor", "--session"], 1)          # another command line: kept
            self.make(root, 104, ["sleep", "5"], 1)
            os.mkdir(os.path.join(root, "notapid"))
            with mock.patch("os.kill") as kill:
                self.assertEqual(lifecycle.reap_orphans(root), 1)
            kill.assert_called_once_with(101, mock.ANY)

    def test_another_users_process_is_never_touched(self):
        with tempfile.TemporaryDirectory() as root:
            self.make(root, 201, list(lifecycle.MONITOR_ARGV), 1)
            with mock.patch("os.kill") as kill:
                self.assertEqual(lifecycle.reap_orphans(root, own_uid=os.getuid() + 1), 0)
            kill.assert_not_called()

    def test_a_missing_or_unreadable_proc_directory_is_not_an_error(self):
        self.assertEqual(lifecycle.reap_orphans("/nonexistent-proc"), 0)

