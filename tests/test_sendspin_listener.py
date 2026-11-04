import asyncio
import socket
import unittest

from piston_device.sendspin import crypto, listener as ls, session as sess
from piston_device.sendspin.sink import FakeSink
from tests.sendspin_peer import FakeServer
from tests.wsclient import WsTestClient


def free_port_block(n: int = 4) -> int:
    """A port P such that P..P+n-1 can all be bound right now."""
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        base = s.getsockname()[1]
        s.close()
        socks = []
        try:
            for p in range(base, base + n):
                t = socket.socket()
                t.bind(("127.0.0.1", p))
                socks.append(t)
            return base
        except OSError:
            continue
        finally:
            for t in socks:
                t.close()
    raise RuntimeError("no free port block")


class Rig:
    def __init__(self, **kw):
        self.changes, self.sinks, self.logs = [], [], []

        def make(reader, writer, *, admit, on_status):
            sink = FakeSink()
            self.sinks.append(sink)
            return sess.SendspinSession(reader, writer, private_key=crypto.generate_private_key(), name="Box", sink=sink,
                                        admit=admit, on_status=on_status, burst_interval_s=0.05)
        self.listener = ls.SendspinListener(make, on_change=lambda l: self.changes.append((l.status, l.server_name, l.port, l.error)),
                                            host="127.0.0.1", log=self.logs.append, **kw)
        self.peers = []

    async def peer(self, activities=("playback",), roles=("player@v1",)) -> FakeServer:
        p = FakeServer()
        await p.connect(self.listener.port)
        await p.handshake()
        await p.hello_and_activate(activities, roles)
        p.start_pump()
        self.peers.append(p)
        return p

    async def close(self):
        for p in self.peers:
            p.close()
        await self.listener.stop()


class ListenerTestCase(unittest.IsolatedAsyncioTestCase):
    async def rig(self, **kw) -> Rig:
        r = Rig(**kw)
        self.addAsyncCleanup(r.close)
        return r

    async def until(self, predicate, timeout=3.0):
        end = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > end:
                raise AssertionError("condition not reached in time")
            await asyncio.sleep(0.01)


class PortTests(ListenerTestCase):
    async def test_it_listens_on_the_start_port_when_free(self):
        base = free_port_block()
        r = await self.rig(start_port=base)
        self.assertTrue(await r.listener.start())
        self.assertEqual((r.listener.port, r.listener.status), (base, "waiting"))

    async def test_a_busy_port_moves_it_to_the_next_free_one(self):
        base = free_port_block()
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", base)
        self.addAsyncCleanup(blocker.wait_closed)
        self.addCleanup(blocker.close)
        r = await self.rig(start_port=base)
        self.assertTrue(await r.listener.start())
        self.assertEqual(r.listener.port, base + 1)
        self.assertTrue(any("busy" in m for m in r.logs))

    async def test_no_free_port_is_an_error_with_the_reason(self):
        base = free_port_block()
        blockers = [await asyncio.start_server(lambda r, w: None, "127.0.0.1", base + i) for i in range(2)]
        self.addCleanup(lambda: [b.close() for b in blockers])
        r = await self.rig(start_port=base, tries=2)
        self.assertFalse(await r.listener.start())
        self.assertEqual((r.listener.status, r.listener.port), ("error", 0))
        self.assertIn(f"{base} to {base + 1}", r.listener.error)

    async def test_start_is_idempotent_and_stop_frees_the_port(self):
        base = free_port_block()
        r = await self.rig(start_port=base)
        await r.listener.start()
        await r.listener.start()
        await r.listener.stop()
        self.assertEqual(r.listener.status, "stopped")
        s = socket.socket()
        s.bind(("127.0.0.1", base))                                              # the port is free again
        s.close()

    async def test_the_default_port_is_8928(self):
        self.assertEqual((ls.DEFAULT_PORT, ls.PATH), (8928, "/sendspin"))


class UpgradeTests(ListenerTestCase):
    async def test_an_upgrade_with_an_origin_header_is_refused(self):
        r = await self.rig(start_port=0)
        await r.listener.start()
        c = await WsTestClient.connect(r.listener.port, "/sendspin", {"Origin": "http://evil.example"})
        self.addCleanup(c.close)
        self.assertIn("403", c.status)

    async def test_the_wrong_path_is_refused(self):
        r = await self.rig(start_port=0)
        await r.listener.start()
        c = await WsTestClient.connect(r.listener.port, "/ws")
        self.addCleanup(c.close)
        self.assertIn("400", c.status)

    async def test_connections_beyond_the_cap_are_closed_at_once(self):
        r = await self.rig(start_port=0, max_connections=2)
        await r.listener.start()
        silent = [await WsTestClient.connect(r.listener.port, "/sendspin") for _ in range(2)]
        self.addCleanup(lambda: [c.close() for c in silent])
        await self.until(lambda: len(r.listener._tasks) == 2)
        with self.assertRaises((asyncio.IncompleteReadError, ConnectionError)):
            await WsTestClient.connect(r.listener.port, "/sendspin")


class StatusTests(ListenerTestCase):
    async def test_status_follows_the_connection(self):
        r = await self.rig(start_port=0)
        await r.listener.start()
        peer = await r.peer()
        await self.until(lambda: r.listener.status == "connected")
        self.assertEqual(r.listener.server_name, "Test Server")
        await peer.send("stream/start", {"server_transmitted": 1, "player": {"codec": "pcm", "sample_rate": 48000, "channels": 2, "bit_depth": 16}})
        await self.until(lambda: r.listener.status == "streaming")
        peer.pump.cancel()
        peer.ws.close()
        await self.until(lambda: r.listener.status == "waiting")
        self.assertIsNone(r.listener.server_name)
        self.assertIsNone(r.listener.current)
        order = [c[0] for c in r.changes]
        self.assertEqual(order, ["waiting", "connected", "streaming", "waiting"])

    async def test_a_sink_error_is_shown_as_an_error_status(self):
        r = await self.rig(start_port=0)
        await r.listener.start()
        peer = await r.peer()
        await self.until(lambda: r.sinks and r.listener.status == "connected")
        r.sinks[0].fail_open = True
        await peer.send("stream/start", {"server_transmitted": 1, "player": {"codec": "pcm", "sample_rate": 48000, "channels": 2, "bit_depth": 16}})
        await self.until(lambda: r.listener.status == "error")
        self.assertIn("fake open failure", r.listener.error)


class AdmissionTests(ListenerTestCase):
    async def test_a_newcomer_with_equal_rank_displaces_the_current_connection(self):
        r = await self.rig(start_port=0)
        await r.listener.start()
        first = await r.peer()
        await self.until(lambda: r.listener.current is not None)
        old = r.listener.current
        second = await r.peer()
        self.assertEqual(await first.next("client/goodbye"), ("client/goodbye", {"reason": "another_server"}))
        await self.until(lambda: r.listener.current is not old and r.listener.current is not None)
        await self.until(lambda: r.listener.status == "connected")             # the displaced session's end did not reset it
        self.assertEqual(r.listener.server_name, "Test Server")

    async def test_a_lower_ranked_newcomer_is_rejected_with_concurrent_attempt(self):
        r = await self.rig(start_port=0)
        await r.listener.start()
        await r.peer()
        await self.until(lambda: r.listener.current is not None)
        current = r.listener.current
        late = await r.peer(activities=(), roles=())
        self.assertEqual(await late.next("client/goodbye"), ("client/goodbye", {"reason": "concurrent_attempt"}))
        await asyncio.sleep(0.1)
        self.assertIs(r.listener.current, current)
        self.assertEqual(r.listener.status, "connected")

    async def test_a_higher_ranked_newcomer_displaces_a_connection_without_activities(self):
        r = await self.rig(start_port=0)
        await r.listener.start()
        idle = await r.peer(activities=(), roles=())
        await self.until(lambda: r.listener.current is not None)
        await r.peer()
        self.assertEqual(await idle.next("client/goodbye"), ("client/goodbye", {"reason": "another_server"}))

    async def test_the_rank_order(self):
        self.assertGreater(ls.rank(["playback"]), ls.rank(["pairing"]))
        self.assertGreater(ls.rank(["pairing"]), ls.rank([]))
        self.assertEqual(ls.rank(["pairing", "playback"]), ls.rank(["playback"]))
        self.assertEqual(ls.rank(["unknown"]), 0)

    async def test_stop_says_goodbye_to_the_connected_server(self):
        r = await self.rig(start_port=0)
        await r.listener.start()
        peer = await r.peer()
        await self.until(lambda: r.listener.status == "connected")
        await r.listener.stop()
        self.assertEqual(await peer.next("client/goodbye"), ("client/goodbye", {"reason": "shutdown"}))
        self.assertEqual((r.listener.status, r.listener.current, r.listener.port), ("stopped", None, 0))
        self.assertFalse(r.listener._tasks)


if __name__ == "__main__":
    unittest.main()
