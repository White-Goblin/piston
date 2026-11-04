"""The aiohttp transport, with a fake `aiohttp` module (it is not installed for the test run)."""
import asyncio
import enum
import importlib
import sys
import types
import unittest

from custom_components.piston import client
from custom_components.piston.const import HEARTBEAT_SECONDS

MODULE = "custom_components.piston.transport"


def fake_aiohttp():
    mod = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    class WSMsgType(enum.IntEnum):
        TEXT, CLOSE, CLOSING, CLOSED, ERROR = 1, 8, 256, 257, 258

    class ClientWSTimeout:
        def __init__(self, ws_close=10.0):
            self.ws_close = ws_close

    mod.ClientError, mod.WSMsgType, mod.ClientWSTimeout = ClientError, WSMsgType, ClientWSTimeout
    mod.ClientWebSocketResponse = mod.ClientSession = object
    return mod


class FakeWs:
    def __init__(self, messages):
        self.messages, self.sent, self.closed = list(messages), [], False

    async def receive(self):
        return self.messages.pop(0)

    async def send_str(self, text):
        self.sent.append(text)

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, ws=None, error=None):
        self.ws, self.error, self.calls = ws, error, []

    async def ws_connect(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.ws


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.aiohttp = fake_aiohttp()
        saved = {k: sys.modules.get(k) for k in ("aiohttp", MODULE)}
        sys.modules["aiohttp"] = self.aiohttp
        sys.modules.pop(MODULE, None)
        self.module = importlib.import_module(MODULE)

        def restore():
            for key, value in saved.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value
        self.addCleanup(restore)

    def msg(self, kind, data=""):
        return types.SimpleNamespace(type=kind, data=data)

    async def test_connect_asks_for_the_heartbeat(self):
        session = FakeSession(FakeWs([]))
        await self.module.AiohttpTransport(session).connect("192.168.1.5", 47830)
        url, kwargs = session.calls[0]
        self.assertEqual((url, kwargs["heartbeat"]), ("ws://192.168.1.5:47830/ws", HEARTBEAT_SECONDS))

    async def test_the_heartbeat_fits_the_devices_own_timings(self):
        # a missed pong closes the socket after 1.5 x the interval; the device drops a silent peer after 60 s
        self.assertLess(HEARTBEAT_SECONDS * 1.5, 60)
        self.assertGreater(HEARTBEAT_SECONDS, 2)

    async def test_ipv6_hosts_are_bracketed(self):
        session = FakeSession(FakeWs([]))
        await self.module.AiohttpTransport(session).connect("fe80::1", 47830)
        self.assertEqual(session.calls[0][0], "ws://[fe80::1]:47830/ws")

    async def test_a_connect_failure_is_an_oserror(self):
        session = FakeSession(error=self.aiohttp.ClientError("refused"))
        with self.assertRaises(OSError):
            await self.module.AiohttpTransport(session).connect("192.168.1.5", 47830)

    async def test_a_socket_closed_by_a_missed_pong_ends_recv_with_connection_closed(self):
        kinds = self.aiohttp.WSMsgType
        for closing in (kinds.CLOSED, kinds.CLOSING, kinds.CLOSE, kinds.ERROR):
            session = FakeSession(FakeWs([self.msg(closing)]))
            conn = await self.module.AiohttpTransport(session).connect("192.168.1.5", 47830)
            with self.assertRaises(client.ConnectionClosed):
                await conn.recv(1)

    async def test_text_frames_are_returned_and_send_failures_become_connection_closed(self):
        kinds = self.aiohttp.WSMsgType
        ws = FakeWs([self.msg(kinds.TEXT, '{"type":"event"}')])
        conn = await self.module.AiohttpTransport(FakeSession(ws)).connect("192.168.1.5", 47830)
        self.assertEqual(await conn.recv(1), '{"type":"event"}')
        await conn.send("hi")
        self.assertEqual(ws.sent, ["hi"])

        async def broken(text):
            raise ConnectionResetError("gone")

        ws.send_str = broken
        with self.assertRaises(client.ConnectionClosed):
            await conn.send("again")


if __name__ == "__main__":
    unittest.main()
