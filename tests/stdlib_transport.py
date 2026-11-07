"""Transport for the integration client built on the stdlib test websocket client."""
import asyncio
import os

from custom_components.piston.client import ConnectionClosed
from . import wsclient


class StdConnection:
    def __init__(self, client):
        self.client = client

    async def send(self, text: str) -> None:
        await self.client.send(text)

    async def recv(self, timeout=None) -> str:
        while True:
            try:
                op, payload = await self.client.recv_raw(timeout if timeout is not None else 3600)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                raise ConnectionClosed("closed") from None
            if op == 1:
                return payload.decode()
            if op == 9:  # answer pings so the server's idle timer stays happy
                mask = os.urandom(4)
                self.client.writer.write(bytes([0x8A, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
            if op == 8:
                raise ConnectionClosed("closed")

    async def close(self) -> None:
        self.client.close()


class StdTransport:
    def __init__(self):
        self.connects = []

    async def connect(self, host, port):
        self.connects.append((host, port))
        client = await wsclient.WsTestClient.connect(port)
        return StdConnection(client)
