import asyncio
import os
import unittest

from piston_device import wsproto
from tests.wsclient import WsTestClient


class WsprotoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = {}
        self.events = []

        async def handle(reader, writer):
            headers = {}
            if not await wsproto.handshake(reader, writer, "/sendspin", headers=headers, forbid=("origin",)):
                writer.close()
                return
            self.seen["headers"] = headers
            try:
                while True:
                    kind, data = await wsproto.read_frame(reader, writer, max_size=200_000)
                    self.events.append((kind, data))
                    if kind == wsproto.OP_BIN:
                        await wsproto.send_binary(writer, data[::-1])
                    elif kind == wsproto.OP_TEXT:
                        await wsproto.send_text(writer, data.decode().upper())
            except wsproto.WsClosed as e:
                self.events.append(("closed", e.code))
            finally:
                writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(self._stop)

    async def _stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_binary_payloads_round_trip_at_every_length_class(self):
        c = await WsTestClient.connect(self.port, "/sendspin")
        self.addCleanup(c.close)
        for size in (0, 1, 125, 126, 3840, 65535, 65536, 150_000):
            data = os.urandom(size)
            await c.send_binary(data)
            op, payload = await c.recv_raw(5)
            self.assertEqual((op, payload), (wsproto.OP_BIN, data[::-1]), msg=f"size {size}")
        self.assertTrue(all(kind == wsproto.OP_BIN for kind, _ in self.events))

    async def test_text_and_binary_can_be_mixed_on_one_connection(self):
        c = await WsTestClient.connect(self.port, "/sendspin")
        self.addCleanup(c.close)
        await c.send("hello")
        self.assertEqual(await c.recv_raw(), (wsproto.OP_TEXT, b"HELLO"))
        await c.send_binary(b"\x00\x01")
        self.assertEqual(await c.recv_raw(), (wsproto.OP_BIN, b"\x01\x00"))

    async def test_an_oversized_message_closes_with_1009(self):
        c = await WsTestClient.connect(self.port, "/sendspin")
        self.addCleanup(c.close)
        await c.send_binary(bytes(300_000))
        await asyncio.sleep(0.2)
        self.assertEqual(self.events[-1], ("closed", 1009))

    async def test_the_caller_can_see_the_request_headers(self):
        c = await WsTestClient.connect(self.port, "/sendspin", headers={"X-Probe": "abc"})
        self.addCleanup(c.close)
        self.assertTrue(c.status.endswith("101 Switching Protocols"))
        self.assertEqual(self.seen["headers"].get("x-probe"), "abc")

    async def test_an_upgrade_with_an_origin_header_is_refused(self):
        c = await WsTestClient.connect(self.port, "/sendspin", headers={"Origin": "http://evil.example"})
        self.addCleanup(c.close)
        self.assertIn("403", c.status)
        self.assertNotIn("headers", self.seen)

    async def test_the_wrong_path_is_refused(self):
        c = await WsTestClient.connect(self.port, "/ws")
        self.addCleanup(c.close)
        self.assertIn("400", c.status)

    async def test_read_message_still_refuses_binary_and_keeps_pong_as_empty_text(self):
        reader = asyncio.StreamReader()
        writer = type("W", (), {"write": lambda self, d: None, "drain": lambda self: asyncio.sleep(0)})()

        def masked(op, data):
            mask = b"\x01\x02\x03\x04"
            return bytes([0x80 | op, 0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))

        reader.feed_data(masked(wsproto.OP_PONG, b""))
        self.assertEqual(await wsproto.read_message(reader, writer), "")
        reader.feed_data(masked(wsproto.OP_BIN, b"xy"))
        with self.assertRaises(wsproto.WsClosed) as cm:
            await wsproto.read_message(reader, writer)
        self.assertEqual(cm.exception.code, 1003)

    def test_unmask_matches_the_byte_loop(self):
        for n in (0, 1, 3, 4, 5, 1000):
            data, mask = os.urandom(n), os.urandom(4)
            self.assertEqual(wsproto._unmask(data, mask), bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


if __name__ == "__main__":
    unittest.main()
