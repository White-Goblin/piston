import asyncio
import unittest
import unittest.mock

from custom_components.piston import client as c
from piston_device.pairing import PairingManager
from piston_device.proto import messages as m
from piston_device.server import ProtocolServer
from .stdlib_transport import StdTransport


class DeviceFixture:
    """A real device-side ProtocolServer with a real PairingManager."""

    def __init__(self, paired_secret=b""):
        self.box = {"secret": paired_secret}
        self.mgr = PairingManager(lambda: self.box["secret"], lambda s: self.box.update(secret=s))
        self.seen = []

        async def echo(data):
            return {"echo": data}

        async def fail(data):
            raise m.ProtocolError(m.NOT_RUNNING, "nothing is playing")

        async def unpair(data):
            self.mgr.unpair()
            return {}

        self.server = ProtocolServer(
            device_id=lambda: "dev123", secret=lambda: self.box["secret"], pairing=self.mgr,
            commands={"echo": echo, "fail": fail, "unpair": unpair},
            snapshot=lambda: {"device": {"name": "box"}}, host="127.0.0.1", port=0, ping_interval=0.1, idle_timeout=5)

    async def start(self):
        await self.server.start()
        return self.server.port


class PairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dev = DeviceFixture()
        self.port = await self.dev.start()
        self.addAsyncCleanup(self.dev.server.stop)
        self.transport = StdTransport()

    async def test_right_code_gives_the_same_secret_as_the_device(self):
        result = await c.pair(self.transport, "127.0.0.1", self.port, self.dev.mgr.code.code)
        self.assertEqual((result.device_id, result.secret), ("dev123", self.dev.box["secret"]))

    async def test_wrong_code(self):
        wrong = "000000" if self.dev.mgr.code.code != "000000" else "111111"
        with self.assertRaises(c.PairingError) as cm:
            await c.pair(self.transport, "127.0.0.1", self.port, wrong)
        self.assertEqual((cm.exception.reason, cm.exception.data["attempts_left"]), ("wrong_code", 4))
        self.assertEqual(self.dev.box["secret"], b"")

    async def test_locked_after_five_wrong_codes(self):
        wrong = "000000" if self.dev.mgr.code.code != "000000" else "111111"
        for _ in range(5):
            with self.assertRaises(c.PairingError):
                await c.pair(self.transport, "127.0.0.1", self.port, wrong)
        with self.assertRaises(c.PairingError) as cm:
            await c.pair(self.transport, "127.0.0.1", self.port, self.dev.mgr.code.code)
        self.assertEqual(cm.exception.reason, "locked")

    async def test_already_paired_device(self):
        self.dev.box["secret"] = b"x" * 32
        with self.assertRaises(c.PairingError) as cm:
            await c.pair(self.transport, "127.0.0.1", self.port, "123456")
        self.assertEqual(cm.exception.reason, "already_paired")

    async def test_version_skew(self):
        with self.assertRaises(c.PairingError) as cm:
            await c.pair(self.transport, "127.0.0.1", self.port, "123456", own=(9, 9))
        self.assertEqual(cm.exception.reason, "version_skew")

    async def test_a_version_2_integration_gets_version_skew(self):
        with self.assertRaises(c.PairingError) as cm:
            await c.pair(self.transport, "127.0.0.1", self.port, "123456", own=(2, 2))
        self.assertEqual(cm.exception.reason, "version_skew")


class SessionTests(unittest.IsolatedAsyncioTestCase):
    SECRET = b"s" * 32

    async def asyncSetUp(self):
        self.dev = DeviceFixture(self.SECRET)
        self.port = await self.dev.start()
        self.addAsyncCleanup(self.dev.server.stop)
        self.transport = StdTransport()
        self.states, self.events, self.connection, self.problems, self.sleeps = [], [], [], [], []
        self.connected = asyncio.Event()

        async def fake_sleep(seconds):
            self.sleeps.append(seconds)
            await asyncio.sleep(0)

        self.fake_sleep = fake_sleep

    def make(self, secret=None, **kw):
        def on_connection(up):
            self.connection.append(up)
            if up:
                self.connected.set()

        client = c.DeckyClient(self.transport, "127.0.0.1", self.port, "dev123", secret or self.SECRET,
                               on_state=lambda n, d: self.states.append((n, d)),
                               on_event=lambda n, d: self.events.append((n, d)), on_connection=on_connection,
                               on_problem=self.problems.append, sleep=self.fake_sleep, **kw)
        self.addAsyncCleanup(client.stop)
        return client

    async def test_connects_receives_snapshot_and_runs_commands(self):
        client = self.make()
        await client.start()
        await asyncio.wait_for(self.connected.wait(), 3)
        self.assertEqual(self.states, [("device", {"name": "box"})])
        self.assertEqual(await client.command("echo", {"a": 1}), {"echo": {"a": 1}})
        with self.assertRaises(c.CommandError) as cm:
            await client.command("fail")
        self.assertEqual(cm.exception.code, m.NOT_RUNNING)

    async def test_commands_fail_fast_when_offline(self):
        client = self.make()
        with self.assertRaises(c.DeviceOffline):
            await client.command("echo")

    async def test_events_are_delivered(self):
        client = self.make()
        await client.start()
        await asyncio.wait_for(self.connected.wait(), 3)
        self.dev.server.push_event("going_to_sleep")
        for _ in range(50):
            if self.events:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.events, [("going_to_sleep", {})])

    # --- sequencing (protocol v2) ---------------------------------------------

    async def started_with_spy(self, **kw):
        """A connected client whose commands are recorded; the fake clock is controlled by the test."""
        self.now = [100.0]
        client = self.make(clock=lambda: self.now[0], **kw)
        self.calls = []
        real = client.command

        async def spy(name, data=None):
            self.calls.append(name)
            return await real(name, data)

        client.command = spy
        await client.start()
        await asyncio.wait_for(self.connected.wait(), 3)
        return client

    async def until(self, condition, tries=100):
        for _ in range(tries):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not reached")

    async def test_in_order_state_frames_are_applied_without_a_resync(self):
        await self.started_with_spy()
        for i in range(3):
            self.dev.server.push_state("device", {"name": f"v{i}"})
        await self.until(lambda: len(self.states) == 4)
        self.assertEqual([d["name"] for _, d in self.states], ["box", "v0", "v1", "v2"])
        self.assertEqual(self.calls, [])

    async def test_stale_or_duplicate_frames_are_dropped(self):
        await self.started_with_spy()
        self.dev.server._seq["device"] = 0
        self.dev.server.push_state("device", {"name": "stale"})   # seq 1 again: not newer than the snapshot
        self.dev.server.push_state("device", {"name": "stale2"})  # seq 2: newer, applied
        await self.until(lambda: len(self.states) == 2)
        self.assertEqual([d["name"] for _, d in self.states], ["box", "stale2"])
        self.assertEqual(self.calls, [])

    async def test_a_gap_is_applied_and_one_resync_requested_and_rate_limited(self):
        await self.started_with_spy()
        self.dev.server._seq["device"] += 4
        self.dev.server.push_state("device", {"name": "after-gap"})
        await self.until(lambda: [d["name"] for _, d in self.states][-1:] == ["box"] and len(self.states) == 3)
        self.assertEqual([d["name"] for _, d in self.states], ["box", "after-gap", "box"])  # snapshot from the resync
        self.assertEqual(self.calls, ["resync"])
        self.dev.server._seq["device"] += 4                         # a second gap within 5 s
        self.dev.server.push_state("device", {"name": "again"})
        await self.until(lambda: len(self.states) == 4)
        await asyncio.sleep(0.1)
        self.assertEqual(self.calls, ["resync"])
        self.now[0] += 6                                            # after the interval a gap is repaired again
        self.dev.server._seq["device"] += 4
        self.dev.server.push_state("device", {"name": "third"})
        await self.until(lambda: self.calls == ["resync", "resync"])

    async def test_a_new_boot_id_resets_tracking_without_a_resync(self):
        await self.started_with_spy()
        self.dev.server.boot_id = "newboot"
        self.dev.server._seq["device"] += 9
        self.dev.server.push_state("device", {"name": "rebooted"})
        await self.until(lambda: len(self.states) == 2)
        self.assertEqual(self.states[-1][1]["name"], "rebooted")
        self.assertEqual(self.calls, [])

    async def test_tracking_resets_on_reconnect(self):
        await self.started_with_spy()
        self.dev.server.push_state("device", {"name": "v"})
        await self.until(lambda: len(self.states) == 2)
        self.connected.clear()
        await self.dev.server.disconnect(1001, "bye")
        await asyncio.wait_for(self.connected.wait(), 3)
        await self.until(lambda: len(self.states) == 3)             # the new snapshot (seq 2 again) is applied
        self.assertEqual(self.states[-1][1]["name"], "box")
        self.assertEqual(self.calls, [])

    async def test_reconnects_after_a_drop_after_the_fast_probe_delay(self):
        client = self.make()
        await client.start()
        await asyncio.wait_for(self.connected.wait(), 3)
        self.connected.clear()
        await self.dev.server.disconnect(1001, "bye")
        await asyncio.wait_for(self.connected.wait(), 3)
        self.assertEqual(self.connection, [True, False, True])
        self.assertEqual(self.sleeps[:1], [c.PROBE_FAST_SECONDS])

    # --- probe policy and wake ---------------------------------------------------

    def build(self, sleep, **kw):
        client = c.DeckyClient(self.transport, "127.0.0.1", self.port, "dev123", self.SECRET,
                               on_state=lambda n, d: self.states.append((n, d)), on_event=lambda n, d: self.events.append((n, d)),
                               on_connection=self.connection.append, on_problem=self.problems.append, sleep=sleep, **kw)
        self.addAsyncCleanup(client.stop)
        return client

    def timed_client(self, **kw):
        """A client whose clock only moves when its sleep is called, so ten minutes pass in no time."""
        self.now = [1000.0]

        async def sleep(seconds):
            self.sleeps.append(seconds)
            self.now[0] += seconds
            await asyncio.sleep(0)

        return self.build(sleep, clock=lambda: self.now[0], **kw)

    def endless_client(self):
        """A client that, once an attempt has failed, waits for ever for the next probe (only wake() moves it on)."""
        blocked = asyncio.Event()

        async def sleep(seconds):
            self.sleeps.append(seconds)
            await blocked.wait()

        return self.build(sleep)

    async def test_a_lost_device_is_probed_every_5_s_for_10_minutes_then_every_30_s(self):
        await self.dev.server.stop()
        client = self.timed_client()
        await client.start()
        for _ in range(300):
            if len(self.sleeps) >= 125:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.sleeps[:120], [5] * 120)
        self.assertEqual(self.sleeps[120:125], [30] * 5)

    async def test_the_fast_window_starts_again_after_a_connection_is_lost(self):
        client = self.timed_client()
        await client.start()
        await self.until(lambda: self.connection == [True])
        self.now[0] += 3600                       # connected for an hour, then it drops
        await self.dev.server.disconnect(1001, "bye")
        await self.until(lambda: self.sleeps[:1] == [5])

    async def test_wake_cuts_the_wait_short(self):
        await self.dev.server.stop()
        client = self.endless_client()
        await client.start()
        await self.until(lambda: len(self.transport.connects) == 1 and self.sleeps)
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.transport.connects), 1)       # waiting for the next probe
        client.wake()
        await self.until(lambda: len(self.transport.connects) == 2)

    async def test_a_wake_during_an_attempt_starts_no_second_one_but_the_next_follows_at_once(self):
        gate = asyncio.Event()
        in_flight = []                                           # sleeps seen so far, noted when each attempt starts

        async def slow_connect(host, port):
            in_flight.append(len(self.sleeps))
            await gate.wait()
            raise OSError("host unreachable")

        self.transport.connect = slow_connect
        client = self.endless_client()
        await client.start()
        await self.until(lambda: len(in_flight) == 1)
        client.wake()                                            # the first attempt is still running
        client.wake()
        await asyncio.sleep(0.05)
        self.assertEqual(len(in_flight), 1)                      # no second attempt alongside it
        gate.set()                                               # it fails; the kept wake makes the next one start without a wait
        await self.until(lambda: len(in_flight) == 2)
        self.assertEqual(in_flight, [0, 0])                      # it never went into the endless sleep in between

    async def test_wake_while_connected_is_ignored(self):
        client = self.timed_client()
        await client.start()
        await self.until(lambda: self.connection == [True])
        client.wake()
        self.assertFalse(client._wake.is_set())

    async def test_a_connect_that_never_answers_is_abandoned_after_the_connect_timeout(self):
        async def hang(host, port):
            await asyncio.Event().wait()

        self.transport.connect = hang
        with unittest.mock.patch.object(c, "PROBE_CONNECT_TIMEOUT", 0.05):
            client = self.timed_client()
            await client.start()
            await self.until(lambda: len(self.sleeps) >= 2)       # attempts keep ending, nothing hangs
        self.assertEqual(self.sleeps[:2], [5, 5])

    async def test_probe_mode_and_failed_attempts_describe_the_connection_for_diagnostics(self):
        await self.dev.server.stop()
        client = self.timed_client()
        self.assertEqual(client.probe_mode, "fast")                    # not started yet: the fast window runs from the start
        await client.start()
        await self.until(lambda: client.failed_attempts >= 3)
        self.assertEqual(client.probe_mode, "fast")
        self.now[0] += 700                                              # gone for over 10 minutes
        self.assertEqual(client.probe_mode, "slow")

    async def test_a_connection_resets_the_failed_attempts_and_reports_the_boot_id(self):
        client = self.timed_client()
        await client.start()
        await self.until(lambda: self.connection == [True])
        self.assertEqual((client.probe_mode, client.failed_attempts), ("connected", 0))
        await self.until(lambda: client.boot_id == self.dev.server.boot_id)

    async def test_probe_mode_is_stopped_after_a_rejected_secret(self):
        client = c.DeckyClient(
            self.transport, "127.0.0.1", self.port, "dev123", b"w" * 32, on_state=lambda n, d: None, on_event=lambda n, d: None,
            on_connection=lambda up: None, on_problem=self.problems.append, sleep=self.fake_sleep)
        self.addAsyncCleanup(client.stop)
        await client.start()
        await asyncio.wait_for(client._task, 3)
        self.assertEqual(client.probe_mode, "stopped")

    async def test_the_address_lookup_is_rate_limited(self):
        await self.dev.server.stop()
        lookups = []

        async def resolver():
            lookups.append(self.now[0])
            return None

        client = self.timed_client(resolver=resolver)
        await client.start()
        await self.until(lambda: len(self.sleeps) >= 25)          # 125 s of probing
        self.assertLessEqual(len(lookups), 3)
        self.assertEqual(lookups[0], 1000.0)                      # the first failure still looks (address changes)

    async def test_resolver_moves_the_client_to_the_new_address(self):
        await self.dev.server.stop()
        dev2 = DeviceFixture(self.SECRET)
        port2 = await dev2.start()
        self.addAsyncCleanup(dev2.server.stop)

        async def resolver():
            return ("127.0.0.1", port2)

        client = self.make(resolver=resolver)
        await client.start()
        await asyncio.wait_for(self.connected.wait(), 3)
        self.assertEqual(self.transport.connects[-1], ("127.0.0.1", port2))

    async def test_wrong_secret_reports_auth_failure_and_stops(self):
        client = self.make(secret=b"w" * 32)
        await client.start()
        await asyncio.wait_for(client._task, 3)
        self.assertIsInstance(self.problems[0], c.AuthFailed)
        self.assertEqual(self.connection, [])
        self.assertEqual(self.sleeps, [])

    async def test_unpaired_device_is_an_auth_failure(self):
        self.dev.box["secret"] = b""
        client = self.make()
        await client.start()
        await asyncio.wait_for(client._task, 3)
        self.assertIn("not paired", str(self.problems[0]))

    async def test_version_skew_is_a_typed_problem_and_retries_slowly(self):
        client = self.make(own=(9, 9))
        await client.start()
        for _ in range(100):
            if self.sleeps:
                break
            await asyncio.sleep(0.01)
        self.assertIsInstance(self.problems[0], c.VersionSkew)
        self.assertEqual(self.problems[0].peer, "old")
        self.assertGreaterEqual(self.sleeps[0], 60)

    async def test_pending_command_fails_when_the_connection_drops(self):
        release = asyncio.Event()

        async def slow(data):
            await release.wait()
            return {}

        self.dev.server.commands["slow"] = slow
        client = self.make()
        await client.start()
        await asyncio.wait_for(self.connected.wait(), 3)
        task = asyncio.create_task(client.command("slow"))
        await asyncio.sleep(0.1)
        await self.dev.server.disconnect(1001, "bye")
        with self.assertRaises(c.DeviceOffline):
            await asyncio.wait_for(task, 3)
        release.set()

    async def test_send_unpair_resets_the_device(self):
        await c.send_unpair(self.transport, "127.0.0.1", self.port, "dev123", self.SECRET)
        self.assertEqual(self.dev.box["secret"], b"")

    async def test_other_device_at_the_address_is_rejected(self):
        with self.assertRaises(c.AuthFailed):
            await c.open_session(self.transport, "127.0.0.1", self.port, "someone-else", self.SECRET)


class LaneTests(unittest.IsolatedAsyncioTestCase):
    """A device with the update lane, a skewed integration and the update frames in both directions."""
    SECRET = b"s" * 32

    async def asyncSetUp(self):
        self.dev = DeviceFixture(self.SECRET)
        self.dev.server._hello_extra = lambda: m.device_hello_extra("0.4.0", 1)
        self.updates, self.statuses, self.hellos, self.problems, self.connection = [], [], [], [], []
        self.dev.server._on_update = self.updates.append
        self.port = await self.dev.start()
        self.addAsyncCleanup(self.dev.server.stop)
        self.transport = StdTransport()
        self.sleeps = []

    async def fake_sleep(self, seconds):
        self.sleeps.append(seconds)
        await asyncio.sleep(0.05)

    def make(self, **kw):
        client = c.DeckyClient(self.transport, "127.0.0.1", self.port, "dev123", self.SECRET,
                               on_state=lambda n, d: None, on_event=lambda n, d: None, on_connection=self.connection.append,
                               on_problem=self.problems.append, on_hello=lambda h, s: self.hellos.append((h, s)),
                               on_update_status=self.statuses.append, sleep=self.fake_sleep, **kw)
        self.addAsyncCleanup(client.stop)
        return client

    async def until(self, cond, seconds=3):
        for _ in range(int(seconds / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not met")

    async def test_a_skewed_device_with_the_lane_is_authenticated_and_can_be_updated(self):
        client = self.make(own=(9, 9))
        await client.start()
        await self.until(lambda: client.lane_connected)
        self.assertEqual(self.hellos[0][0]["plugin_version"], "0.4.0")
        self.assertEqual(self.hellos[0][1], "old")
        self.assertTrue(isinstance(self.problems[0], c.VersionSkew) and self.problems[0].updatable)
        self.assertEqual(self.connection, [])                       # never a regular connection
        await self.until(lambda: self.statuses)                     # the lane greeting
        await client.send_update("1.2.3", "https://x/y.zip", "a" * 64)
        await self.until(lambda: self.updates)
        self.assertEqual(self.updates[0]["version"], "1.2.3")
        self.dev.server.push_update_status("downloading", "1.2.3")
        await self.until(lambda: self.statuses[-1]["state"] == "downloading")

    async def test_a_normal_connection_reports_the_hello_and_carries_updates(self):
        client = self.make()
        await client.start()
        await self.until(lambda: self.connection == [True])
        self.assertEqual((self.hellos[0][0]["plugin_version"], self.hellos[0][1]), ("0.4.0", None))
        self.assertFalse(client.lane_connected)
        await client.send_update("1.2.3", "https://x/y.zip", "a" * 64)
        await self.until(lambda: self.updates)
        self.dev.server.push_update_status("installing", "1.2.3")
        await self.until(lambda: self.statuses and self.statuses[-1]["state"] == "installing")

    async def test_a_skewed_device_without_the_lane_is_a_plain_version_skew(self):
        self.dev.server._hello_extra = lambda: {}
        client = self.make(own=(9, 9))
        await client.start()
        await self.until(lambda: self.problems)
        self.assertFalse(self.problems[0].updatable)
        self.assertFalse(client.lane_connected)
        self.assertGreaterEqual(self.sleeps[0], 60)

    async def test_send_update_offline_raises(self):
        client = self.make()
        with self.assertRaises(c.DeviceOffline):
            await client.send_update("1.2.3", "https://x/y.zip", "a" * 64)

    async def test_the_lane_retries_quickly_while_an_update_runs_and_slowly_otherwise(self):
        client = self.make(own=(9, 9))
        await client.start()
        await self.until(lambda: client.lane_connected)
        self.dev.server.push_update_status("installing", "1.2.3")
        await self.until(lambda: self.statuses[-1]["state"] == "installing")
        await self.dev.server.stop()                                 # the device restarts for the install
        await self.until(lambda: self.sleeps)
        self.assertEqual(self.sleeps[0], c.PROBE_FAST_SECONDS)

    async def test_closing_the_lane_is_reported(self):
        closed = []
        client = self.make(own=(9, 9), on_lane_closed=lambda: closed.append(True))
        await client.start()
        await self.until(lambda: client.lane_connected)
        await self.dev.server.stop()
        await self.until(lambda: closed)
        self.assertFalse(client.lane_connected)

    async def test_open_session_still_raises_version_skew_for_a_lane_device(self):
        with self.assertRaises(c.VersionSkew) as cm:
            await c.open_session(self.transport, "127.0.0.1", self.port, "dev123", self.SECRET, own=(9, 9))
        self.assertTrue(cm.exception.updatable)
