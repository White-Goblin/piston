"""Tiny websocket test client (masked frames) for exercising decky_ha.server."""
import asyncio
import base64
import json
import os
import struct


class WsTestClient:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, port: int, path: str = "/ws"):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        client = cls(reader, writer)
        client.status = head.split(b"\r\n")[0].decode()
        return client

    async def send(self, obj, opcode: int = 1) -> None:
        payload = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack("!H", n))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.writer.write(head + mask + masked)
        await self.writer.drain()

    async def recv_raw(self, timeout: float = 3):
        """(opcode, payload) of the next server frame."""
        async def read():
            b0, b1 = await self.reader.readexactly(2)
            n = b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack("!H", await self.reader.readexactly(2))
            return b0 & 0x0F, await self.reader.readexactly(n)
        return await asyncio.wait_for(read(), timeout)

    async def recv(self, timeout: float = 3):
        """Next JSON frame; returns ("close", code) for a close frame."""
        while True:
            op, payload = await self.recv_raw(timeout)
            if op == 1:
                return json.loads(payload)
            if op == 8:
                return ("close", struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005)

    def close(self) -> None:
        self.writer.close()
