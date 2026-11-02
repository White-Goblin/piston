import asyncio
import unittest

from custom_components.decky_home_assistant import client as c
from decky_ha.pairing import PairingManager
from decky_ha.proto import messages as m
from decky_ha.server import ProtocolServer
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

    async def test_reconnects_after_a_drop_with_backoff_that_resets(self):
        client = self.make()
        await client.start()
        await asyncio.wait_for(self.connected.wait(), 3)
        self.connected.clear()
        await self.dev.server.disconnect(1001, "bye")
        await asyncio.wait_for(self.connected.wait(), 3)
        self.assertEqual(self.connection, [True, False, True])
        self.assertEqual(self.sleeps[:1], [2])

    async def test_backoff_doubles_while_unreachable_then_caps(self):
        await self.dev.server.stop()
        client = self.make()
        await client.start()
        for _ in range(200):
            if len(self.sleeps) >= 9:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.sleeps[:9], [2, 4, 8, 16, 32, 64, 128, 256, 300])

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
