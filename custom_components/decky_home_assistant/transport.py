"""aiohttp implementation of the client's Transport (what runs inside Home Assistant)."""
from __future__ import annotations

import asyncio

import aiohttp

from .client import ConnectionClosed


class AiohttpConnection:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self._ws = ws

    async def send(self, text: str) -> None:
        try:
            await self._ws.send_str(text)
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as e:
            raise ConnectionClosed(str(e)) from None

    async def recv(self, timeout: float | None = None) -> str:
        while True:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout)
            except (aiohttp.ClientError, ConnectionError) as e:
                raise ConnectionClosed(str(e)) from None
            if msg.type == aiohttp.WSMsgType.TEXT:
                return msg.data
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
                raise ConnectionClosed("closed")

    async def close(self) -> None:
        await self._ws.close()


class AiohttpTransport:
    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def connect(self, host: str, port: int) -> AiohttpConnection:
        host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        try:
            ws = await self._session.ws_connect(f"ws://{host}:{port}/ws", timeout=aiohttp.ClientWSTimeout(ws_close=5)
                                                if hasattr(aiohttp, "ClientWSTimeout") else 5, heartbeat=None)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            raise OSError(str(e)) from None
        return AiohttpConnection(ws)
