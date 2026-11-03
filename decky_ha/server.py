"""Authenticated websocket server for the LAN protocol (see docs/protocol.md).

One authenticated connection at a time: a new one replaces the old. Commands run one at a time;
a newer `launch` supersedes a queued older one.
"""
import asyncio
import logging
import secrets
from typing import Awaitable, Callable

from . import wsproto
from .proto import PROTOCOL_VERSION, MIN_PROTOCOL_VERSION, auth, messages as m

log = logging.getLogger("decky-ha.server")

CLOSE_VERSION, CLOSE_REPLACED, CLOSE_UNAUTHORIZED, CLOSE_BAD_REQUEST = 4000, 4001, 4002, 4003
DEFAULT_PORT = 47830
RESYNC = "resync"  # built-in command: answer ok, then send every state group again

Handler = Callable[[dict], Awaitable[dict | None]]


class _Conn:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.pending: list = []  # queued cmd frames not yet started
        self.wake = asyncio.Event()
        self.closed = False

    def send(self, frame: dict) -> None:
        if not self.closed:
            self.writer.write(wsproto.frame(wsproto.OP_TEXT, m.encode(frame).encode()))


class ProtocolServer:
    def __init__(self, *, device_id: Callable[[], str], secret: Callable[[], bytes], pairing,
                 commands: dict, snapshot: Callable[[], dict], host: str = "0.0.0.0", port: int = DEFAULT_PORT,
                 ping_interval: float = 20, idle_timeout: float = 60, hello_timeout: float = 10,
                 on_connection: Callable[[bool], None] | None = None,
                 hello_extra: Callable[[], dict] | None = None):
        self._device_id, self._secret, self.pairing = device_id, secret, pairing
        self.commands: dict[str, Handler] = commands
        self._snapshot = snapshot
        self.host, self.port = host, port
        self._ping_interval, self._idle_timeout, self._hello_timeout = ping_interval, idle_timeout, hello_timeout
        self._on_connection = on_connection
        self._hello_extra = hello_extra or (lambda: {})
        self._server = None
        self.conn: _Conn | None = None
        # Sequencing (protocol v2): a state frame carries this run's boot_id and a per-group counter
        self.boot_id = secrets.token_hex(8)
        self._seq: dict[str, int] = {}

    @property
    def connected(self) -> bool:
        return self.conn is not None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("protocol server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        if self.conn is not None:
            await self._close(self.conn, 1001, "going away")

    # --- pushes to the connected integration ------------------------------

    def push_state(self, name: str, data: dict) -> None:
        self._seq[name] = self._seq.get(name, 0) + 1
        if self.conn is not None:
            self.conn.send(m.state(name, data, self.boot_id, self._seq[name]))

    def _snapshot_frame(self, name: str, data: dict) -> dict:
        """A snapshot carries the group's current seq without counting as a new push (a never-pushed group gets seq 1)."""
        seq = self._seq[name] = max(self._seq.get(name, 0), 1)
        return m.state(name, data, self.boot_id, seq)

    def push_event(self, name: str, data: dict | None = None) -> None:
        if self.conn is not None:
            self.conn.send(m.event(name, data))

    async def flush(self) -> None:
        """Wait until everything sent so far has left the socket buffer (used just before the device goes down)."""
        conn = self.conn
        if conn is not None and not conn.closed:
            try:
                await conn.writer.drain()
            except (ConnectionError, OSError):
                pass

    async def disconnect(self, code: int = CLOSE_UNAUTHORIZED, reason: str = "") -> None:
        if self.conn is not None:
            await self._close(self.conn, code, reason)

    # --- connection handling ----------------------------------------------

    async def _close(self, conn: _Conn, code: int, reason: str = "") -> None:
        if not conn.closed:
            await wsproto.send_close(conn.writer, code, reason)
            conn.closed = True
            conn.writer.close()

    async def _client(self, reader, writer) -> None:
        conn = _Conn(reader, writer)
        try:
            if not await wsproto.handshake(reader, writer):
                writer.close()
                return
            await self._session(conn)
        except wsproto.WsClosed as e:
            log.debug("connection closed: %s", e)
        except Exception:  # noqa: BLE001 - a bad peer must never take the server down
            log.exception("connection failed")
        finally:
            conn.closed = True
            writer.close()
            if self.conn is conn:
                self.conn = None
                if self._on_connection:
                    self._on_connection(False)

    async def _read(self, conn: _Conn, timeout: float) -> dict:
        """Next non-pong frame, validated. Raises WsClosed on timeout or close."""
        while True:
            try:
                text = await asyncio.wait_for(wsproto.read_message(conn.reader, conn.writer), timeout)
            except asyncio.TimeoutError:
                raise wsproto.WsClosed(1001, "idle timeout") from None
            if text == "":
                continue  # pong
            try:
                return m.parse(text)
            except m.ProtocolError as e:
                conn.send(m.result_error(0, e.code, e.message))
                raise wsproto.WsClosed(CLOSE_BAD_REQUEST, e.message) from None

    async def _session(self, conn: _Conn) -> None:
        secret = self._secret()
        conn.send({**m.hello(self._device_id()), "paired": bool(secret), **self._hello_extra()})
        first = await self._read(conn, self._hello_timeout)
        if first["type"] != "hello":
            raise wsproto.WsClosed(CLOSE_BAD_REQUEST, "hello expected")
        try:
            m.negotiate(PROTOCOL_VERSION, MIN_PROTOCOL_VERSION, first["proto"], first["min_proto"])
        except m.ProtocolError as e:
            conn.send({**m.result_error(0, e.code, e.message), "peer": e.data.get("peer")})
            await self._close(conn, CLOSE_VERSION, e.message)
            return

        if secret:
            await self._authenticate(conn, secret)
        else:
            await self._pair(conn)
            secret = self._secret()
            if not secret:
                return
        await self._serve(conn)

    async def _authenticate(self, conn: _Conn, secret: bytes) -> None:
        nonce_d = auth.make_challenge()
        conn.send({"type": "challenge", "nonce": nonce_d})
        reply = await self._read(conn, self._hello_timeout)
        nonce_h, proof = reply.get("nonce"), reply.get("proof")
        if (reply["type"] != "proof" or not isinstance(nonce_h, str) or len(nonce_h) > 64
                or not auth.verify(secret, nonce_d, nonce_h, auth.ROLE_HA, proof)):
            conn.send(m.result_error(0, m.UNAUTHORIZED, "authentication failed"))
            await self._close(conn, CLOSE_UNAUTHORIZED, "authentication failed")
            raise wsproto.WsClosed(CLOSE_UNAUTHORIZED, "authentication failed")
        conn.send({"type": "proof", "proof": auth.prove(secret, nonce_d, nonce_h, auth.ROLE_DEVICE)})

    async def _pair(self, conn: _Conn) -> None:
        """Unpaired device: only pairing frames are accepted until the secret exists."""
        while not self._secret():
            frame = await self._read(conn, self._idle_timeout)
            if frame["type"] != "pair":
                conn.send(m.result_error(0, m.UNAUTHORIZED, "pairing required"))
                await self._close(conn, CLOSE_UNAUTHORIZED, "pairing required")
                raise wsproto.WsClosed(CLOSE_UNAUTHORIZED, "pairing required")
            try:
                conn.send(await self.pairing.handle(frame))
            except m.ProtocolError as e:
                conn.send({"type": "pair", "step": "error", "code": e.code, "message": e.message, **e.data})

    async def _serve(self, conn: _Conn) -> None:
        old, self.conn = self.conn, conn
        if old is not None:
            await self._close(old, CLOSE_REPLACED, "replaced by a new connection")
        if self._on_connection:
            self._on_connection(True)
        for name, data in self._snapshot().items():
            conn.send(self._snapshot_frame(name, data))
        worker = asyncio.create_task(self._worker(conn))
        pinger = asyncio.create_task(self._pinger(conn))
        try:
            while True:
                frame = await self._read(conn, self._idle_timeout)
                if frame["type"] != "cmd":
                    conn.send(m.result_error(0, m.BAD_REQUEST, "only commands are accepted"))
                    continue
                self._enqueue(conn, frame)
        finally:
            worker.cancel()
            pinger.cancel()
            for frame in conn.pending:
                conn.send(m.result_error(frame["id"], m.OFFLINE, "connection closed"))
            conn.pending.clear()

    def _send_resync(self, conn: _Conn) -> None:
        """Every group again, each counting as a new push so the receiver applies it by its normal seq rule."""
        for name, data in self._snapshot().items():
            self._seq[name] = self._seq.get(name, 0) + 1
            conn.send(m.state(name, data, self.boot_id, self._seq[name]))

    async def _pinger(self, conn: _Conn) -> None:
        while not conn.closed:
            await asyncio.sleep(self._ping_interval)
            try:
                await wsproto.send_ping(conn.writer)
            except (ConnectionError, OSError):
                return

    # --- command queue ------------------------------------------------------

    def _enqueue(self, conn: _Conn, frame: dict) -> None:
        if frame["name"] == RESYNC:
            if any(f["name"] == RESYNC for f in conn.pending):
                conn.send(m.result_ok(frame["id"], {"coalesced": True}))  # the queued one will send the snapshot
                return
        if frame["name"] == "launch":
            for old in [f for f in conn.pending if f["name"] == "launch"]:
                conn.pending.remove(old)
                conn.send(m.result_error(old["id"], m.BUSY, "superseded by a newer launch"))
        conn.pending.append(frame)
        conn.wake.set()

    async def _worker(self, conn: _Conn) -> None:
        while True:
            while not conn.pending:
                conn.wake.clear()
                await conn.wake.wait()
            frame = conn.pending.pop(0)
            if frame["name"] == RESYNC:
                conn.send(m.result_ok(frame["id"]))
                self._send_resync(conn)
                continue
            handler = self.commands.get(frame["name"])
            if handler is None:
                conn.send(m.result_error(frame["id"], m.UNSUPPORTED, f"unknown command {frame['name']}"))
                continue
            try:
                conn.send(m.result_ok(frame["id"], await handler(frame["data"]) or {}))
            except m.ProtocolError as e:
                conn.send(m.result_error(frame["id"], e.code, e.message))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("command %s failed", frame["name"])
                conn.send(m.result_error(frame["id"], m.INTERNAL, str(e)))
