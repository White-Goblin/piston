"""Minimal RFC 6455 server side over asyncio streams: handshake, text frames, ping/pong, close."""
import asyncio
import base64
import hashlib
import struct

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 1_000_000


class WsClosed(Exception):
    """The peer closed the connection (or sent something we must close over)."""

    def __init__(self, code: int = 1006, reason: str = ""):
        super().__init__(f"closed {code} {reason}".strip())
        self.code, self.reason = code, reason


async def handshake(reader, writer, path: str = "/ws", timeout: float = 5) -> bool:
    """Read the HTTP upgrade request and answer it. Returns False (after replying) if it is not ours."""
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
        return False
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split()
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    ok = (
        len(parts) == 3 and parts[0] == "GET" and parts[1].split("?")[0] == path
        and headers.get("upgrade", "").lower() == "websocket"
        and "upgrade" in headers.get("connection", "").lower()
        and headers.get("sec-websocket-version") == "13"
        and headers.get("sec-websocket-key")
    )
    if not ok:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await _drain(writer)
        return False
    accept = base64.b64encode(hashlib.sha1(headers["sec-websocket-key"].encode() + GUID).digest()).decode()
    writer.write(
        ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
         f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
    )
    await _drain(writer)
    return True


async def _drain(writer) -> None:
    try:
        await writer.drain()
    except ConnectionError:
        pass


def frame(opcode: int, payload: bytes = b"") -> bytes:
    """One unmasked, unfragmented server frame."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


async def send_text(writer, text: str) -> None:
    writer.write(frame(OP_TEXT, text.encode()))
    await writer.drain()


async def send_ping(writer) -> None:
    writer.write(frame(OP_PING))
    await writer.drain()


async def send_close(writer, code: int = 1000, reason: str = "") -> None:
    try:
        writer.write(frame(OP_CLOSE, struct.pack("!H", code) + reason.encode()[:100]))
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def read_message(reader, writer, max_size: int = MAX_MESSAGE) -> str:
    """Next complete text message. Answers pings; raises WsClosed on close, error or oversize.

    Pongs are returned to the caller as an empty string so it can count them as activity."""
    data, kind = b"", None
    while True:
        try:
            b0, b1 = await reader.readexactly(2)
            n = b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack("!H", await reader.readexactly(2))
            elif n == 127:
                (n,) = struct.unpack("!Q", await reader.readexactly(8))
            if n > max_size or len(data) + n > max_size:
                raise WsClosed(1009, "message too big")
            if not b1 & 0x80:
                raise WsClosed(1002, "client frames must be masked")
            mask = await reader.readexactly(4)
            payload = bytearray(await reader.readexactly(n))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            raise WsClosed(1006, "connection lost") from None
        for i in range(n):
            payload[i] ^= mask[i % 4]
        op, fin = b0 & 0x0F, bool(b0 & 0x80)
        if op == OP_PING:
            writer.write(frame(OP_PONG, bytes(payload)))
            await _drain(writer)
            continue
        if op == OP_PONG:
            return ""
        if op == OP_CLOSE:
            code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
            await send_close(writer, 1000)
            raise WsClosed(code)
        if op in (OP_TEXT, OP_BIN):
            kind, data = op, bytes(payload)
        elif op == OP_CONT and kind is not None:
            data += bytes(payload)
        else:
            raise WsClosed(1002, "unexpected frame")
        if fin:
            if kind != OP_TEXT:
                raise WsClosed(1003, "text only")
            try:
                return data.decode()
            except UnicodeDecodeError:
                raise WsClosed(1007, "invalid text") from None
