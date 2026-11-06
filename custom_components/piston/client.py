"""Client for the device's LAN protocol: pairing, authenticated session, commands, reconnect.

Independent of Home Assistant: the websocket itself comes from a `Transport` (aiohttp in Home Assistant,
a stdlib one in tests). See docs/protocol.md.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from .proto import MIN_PROTOCOL_VERSION, PROTOCOL_VERSION, auth, messages as m, pake

_LOGGER = logging.getLogger(__name__)

HA_ID = "homeassistant"
CONNECT_TIMEOUT = 10
COMMAND_TIMEOUT = 30
# Reconnect policy: a device that sleeps or is off is probed often right after it went away (a wake is the usual
# reason it comes back) and less often once it has been gone for a while. Protocol errors have their own delays.
PROBE_FAST_SECONDS, PROBE_SLOW_SECONDS, PROBE_FAST_WINDOW = 5, 30, 600
VERSION_SKEW_DELAY = 60
PROBE_CONNECT_TIMEOUT = 2   # a sleeping host does not answer a connect; do not wait for the OS to give up
RESOLVE_INTERVAL = 60       # the mDNS lookup can take 3 s when the device is away, so not on every probe
RESYNC_MIN_INTERVAL = 5  # seconds between resync requests


class ConnectionClosed(Exception):
    """The websocket ended (raised by a Connection)."""


class DeviceOffline(Exception):
    """No authenticated connection to the device right now."""


class CommandError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class VersionSkew(Exception):
    """`peer` is "old" (the device plugin needs updating) or "new" (this integration needs updating)."""

    def __init__(self, peer: str, updatable: bool = False):
        super().__init__(f"device is too {peer}")
        self.peer = peer
        self.updatable = updatable  # True: the device is authenticated and has the update lane open (see docs/protocol.md)


class AuthFailed(Exception):
    """The device rejected the stored secret, is unpaired, or is a different device."""


class PairingError(Exception):
    """`reason` is wrong_code, expired, locked, already_paired, version_skew or protocol."""

    def __init__(self, reason: str, data: dict | None = None):
        super().__init__(reason)
        self.reason, self.data = reason, data or {}


class Connection(Protocol):
    async def send(self, text: str) -> None: ...
    async def recv(self, timeout: float | None = None) -> str: ...  # raises ConnectionClosed, asyncio.TimeoutError
    async def close(self) -> None: ...


class Transport(Protocol):
    async def connect(self, host: str, port: int) -> Connection: ...


async def _recv_frame(conn: Connection, timeout: float | None) -> dict:
    try:
        return m.parse(await conn.recv(timeout))
    except m.ProtocolError as e:
        raise ConnectionClosed(f"bad frame: {e.message}") from None


async def _read_hello(conn: Connection) -> dict:
    hello = await _recv_frame(conn, CONNECT_TIMEOUT)
    if hello["type"] != "hello":
        raise ConnectionClosed("hello expected")
    return hello


async def _hello(conn: Connection, own: tuple[int, int]) -> dict:
    """Read the device's hello, send ours, check the versions."""
    hello = await _recv_frame(conn, CONNECT_TIMEOUT)
    if hello["type"] != "hello":
        raise ConnectionClosed("hello expected")
    await conn.send(m.encode(m.hello(HA_ID, *own)))
    try:
        m.negotiate(own[0], own[1], hello["proto"], hello["min_proto"])
    except m.ProtocolError as e:
        raise VersionSkew(e.data["peer"]) from None
    return hello


@dataclass
class Session:
    """An authenticated connection. `skew` is None for a normal one, or "old"/"new" when the protocol versions
    differ but the device has the update lane (only `update` / `update_status` frames flow then)."""
    conn: Connection
    hello: dict
    skew: str | None = None


async def open_session(transport: Transport, host: str, port: int, device_id: str, secret: bytes,
                       own: tuple[int, int] = (PROTOCOL_VERSION, MIN_PROTOCOL_VERSION),
                       connect_timeout: float | None = None) -> Connection:
    """Connect and authenticate with the stored secret. The caller owns the returned connection.

    Raises VersionSkew when the protocol versions differ (use `open_session_ex` to keep such a connection for updates)."""
    session = await open_session_ex(transport, host, port, device_id, secret, own, connect_timeout)
    if session.skew is not None:
        await session.conn.close()
        raise VersionSkew(session.skew, updatable=True)
    return session.conn


async def open_session_ex(transport: Transport, host: str, port: int, device_id: str, secret: bytes,
                          own: tuple[int, int] = (PROTOCOL_VERSION, MIN_PROTOCOL_VERSION),
                          connect_timeout: float | None = None) -> Session:
    """Connect and authenticate (before the versions are compared, so a skewed device with the lane can still be updated)."""
    conn = await asyncio.wait_for(transport.connect(host, port), connect_timeout)
    try:
        hello = await _read_hello(conn)
        await conn.send(m.encode(m.hello(HA_ID, *own)))
        skew = None
        try:
            m.negotiate(own[0], own[1], hello["proto"], hello["min_proto"])
        except m.ProtocolError as e:
            if not hello.get("lane"):
                raise VersionSkew(e.data["peer"]) from None  # no lane: nothing to talk about
            skew = e.data["peer"]
        if hello["device_id"] != device_id:
            raise AuthFailed("a different device answered at this address")
        if not hello.get("paired"):
            raise AuthFailed("the device is not paired")
        challenge = await _recv_frame(conn, CONNECT_TIMEOUT)
        if challenge["type"] == "result" and not challenge["ok"]:
            err = challenge["error"]
            if err["code"] == m.VERSION_SKEW:
                raise VersionSkew(challenge.get("peer") or "old")
            raise AuthFailed(err.get("message", err["code"]))
        if challenge["type"] != "challenge":
            raise ConnectionClosed("challenge expected")
        nonce_h = auth.make_challenge()
        await conn.send(m.encode({"type": "proof", "nonce": nonce_h,
                                  "proof": auth.prove(secret, challenge["nonce"], nonce_h, auth.ROLE_HA)}))
        reply = await _recv_frame(conn, CONNECT_TIMEOUT)
        if reply["type"] == "result" and not reply["ok"]:
            raise AuthFailed(reply["error"].get("message", "authentication failed"))
        if reply["type"] != "proof" or not auth.verify(secret, challenge["nonce"], nonce_h, auth.ROLE_DEVICE,
                                                       reply.get("proof")):
            raise AuthFailed("the device did not prove it knows the secret")
        return Session(conn, hello, skew)
    except BaseException:
        await conn.close()
        raise


@dataclass
class PairResult:
    device_id: str
    secret: bytes
    name: str = ""


async def probe(transport: Transport, host: str, port: int) -> dict:
    """Read a device's hello (device_id, name, paired, protocol) without authenticating."""
    conn = await transport.connect(host, port)
    try:
        hello = await _recv_frame(conn, CONNECT_TIMEOUT)
        if hello["type"] != "hello":
            raise ConnectionClosed("hello expected")
        return hello
    finally:
        await conn.close()


async def pair(transport: Transport, host: str, port: int, code: str,
               own: tuple[int, int] = (PROTOCOL_VERSION, MIN_PROTOCOL_VERSION)) -> PairResult:
    """Run the pairing exchange with the code shown on the device. Raises PairingError."""
    conn = await transport.connect(host, port)
    try:
        try:
            hello = await _hello(conn, own)
        except VersionSkew:
            raise PairingError("version_skew") from None
        if hello.get("paired"):
            raise PairingError("already_paired")
        await conn.send(m.encode({"type": "pair", "step": "begin"}))
        challenge = await _pair_reply(conn, "challenge")
        client = pake.SrpClient(code)
        try:
            m1 = client.process(bytes.fromhex(challenge["salt"]), pake.from_hex(challenge["B"]))
        except (ValueError, KeyError, pake.PakeError):
            raise PairingError("protocol") from None
        await conn.send(m.encode({"type": "pair", "step": "proof", "A": pake.to_hex(client.A), "M1": m1.hex()}))
        done = await _pair_reply(conn, "done")
        try:
            client.confirm(bytes.fromhex(done["M2"]))
        except (ValueError, KeyError, pake.PakeError):
            raise PairingError("protocol") from None
        return PairResult(hello["device_id"], client.secret, str(hello.get("name") or ""))
    except ConnectionClosed:
        raise PairingError("protocol") from None
    finally:
        await conn.close()


async def _pair_reply(conn: Connection, step: str) -> dict:
    frame = await _recv_frame(conn, CONNECT_TIMEOUT)
    if frame["type"] == "pair" and frame.get("step") == "error":
        raise PairingError(frame.get("reason") or "protocol", {k: v for k, v in frame.items() if k in ("attempts_left", "retry_in")})
    if frame["type"] == "pair" and frame.get("step") == step:
        return frame
    raise PairingError("protocol")


async def send_unpair(transport: Transport, host: str, port: int, device_id: str, secret: bytes) -> None:
    """Tell a (possibly sleeping) device to forget this pairing. Raises if it cannot be reached."""
    conn = await open_session(transport, host, port, device_id, secret)
    try:
        await conn.send(m.encode(m.cmd(1, "unpair")))
        for _ in range(50):  # skip the state snapshot until our result arrives
            frame = await _recv_frame(conn, CONNECT_TIMEOUT)
            if frame["type"] == "result" and frame["id"] == 1:
                return
    finally:
        await conn.close()


INPUT_MAX_PENDING = 256  # events waiting behind a slow socket; beyond this the oldest moves and scrolls are dropped


class InputOutbox:
    """Input events waiting to be written. Adjacent moves (and adjacent scrolls) are summed so a slow socket never builds a
    backlog of pointer motion; clicks, keys, text, actions and releases keep their order and are never merged or dropped
    (except a flood of more than INPUT_MAX_PENDING events, where the oldest moves and scrolls go first)."""

    def __init__(self, max_pending: int = INPUT_MAX_PENDING):
        self._items: list[dict] = []
        self._max = max_pending
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    def add(self, events: list[dict]) -> None:
        for event in events:
            kind = event["k"]
            last = self._items[-1] if self._items else None
            if last is not None and last["k"] == kind and kind in ("move", "scroll"):
                last["dx"] += event["dx"]
                last["dy"] += event["dy"]
            else:
                self._items.append(dict(event))
        if len(self._items) > self._max:
            self._shed()

    def _shed(self) -> None:
        keep = []
        excess = len(self._items) - self._max
        for event in self._items:
            if excess > 0 and event["k"] in ("move", "scroll"):
                excess -= 1
                self.dropped += 1
                continue
            keep.append(event)
        self._items = keep

    def take(self, limit: int = m.MAX_INPUT_EVENTS) -> list[dict]:
        batch, self._items = self._items[:limit], self._items[limit:]
        return batch

    def clear(self) -> None:
        self._items = []


class DeckyClient:
    """Keeps an authenticated session open, delivers state and events, and runs commands."""

    def __init__(self, transport: Transport, host: str, port: int, device_id: str, secret: bytes, *,
                 on_state: Callable[[str, dict], None], on_event: Callable[[str, dict], None],
                 on_connection: Callable[[bool], None], on_problem: Callable[[Exception], None] = lambda e: None,
                 on_hello: Callable[[dict, str | None], None] = lambda hello, skew: None,
                 on_update_status: Callable[[dict], None] = lambda frame: None,
                 on_lane_closed: Callable[[], None] = lambda: None,
                 resolver: Callable[[], Awaitable[tuple[str, int] | None]] | None = None,
                 own: tuple[int, int] = (PROTOCOL_VERSION, MIN_PROTOCOL_VERSION),
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, command_timeout: float = COMMAND_TIMEOUT,
                 clock: Callable[[], float] = time.monotonic):
        self._transport, self.host, self.port = transport, host, port
        self._device_id, self._secret = device_id, secret
        self._on_state, self._on_event, self._on_connection, self._on_problem = on_state, on_event, on_connection, on_problem
        self._resolver, self._own, self._sleep, self._command_timeout = resolver, own, sleep, command_timeout
        self._on_hello, self._on_update_status, self._on_lane_closed = on_hello, on_update_status, on_lane_closed
        self._conn: Connection | None = None
        self._lane: Connection | None = None   # authenticated but version-skewed: only update frames flow
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._clock = clock
        self._boot_id: str | None = None  # sequencing, reset on every connection
        self._last_seq: dict[str, int] = {}
        self._resync_task: asyncio.Task | None = None
        self._last_resync = float("-inf")
        self._wake = asyncio.Event()      # set by wake(): the device was seen again, do not wait for the next probe
        self._attempting = False
        self._lost_at = self._clock()
        self.failed_attempts = 0          # consecutive failed attempts since the last connection (for diagnostics)
        self._outbox = InputOutbox()      # remote input waiting for the socket (see write_input)
        self._input_task: asyncio.Task | None = None
        self._input_wake = asyncio.Event()
        self._input_seq = 0
        self.input_frames = 0             # for diagnostics: frames written / events dropped under backpressure
        self._last_resolve = float("-inf")

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._resync_task is not None:
            self._resync_task.cancel()
        if self._input_task is not None:
            self._input_task.cancel()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self._fail_pending()

    def set_address(self, host: str, port: int) -> None:
        self.host, self.port = host, port

    @property
    def boot_id(self) -> str | None:
        return self._boot_id

    @property
    def probe_mode(self) -> str:
        """"connected", "fast" or "slow" (the probe cadence), or "stopped" when probing ended (rejected secret)."""
        if self._conn is not None:
            return "connected"
        if self._task is not None and self._task.done():
            return "stopped"
        return "fast" if self._clock() - self._lost_at < PROBE_FAST_WINDOW else "slow"

    def wake(self) -> None:
        """The device was seen on the network (its mDNS announcement): connect now instead of at the next probe.

        Never starts a second attempt: during one, the signal is kept and the next attempt follows at once if it fails."""
        if self._conn is None:   # while connected there is nothing to hurry
            self._wake.set()

    @property
    def lane_connected(self) -> bool:
        return self._lane is not None

    async def send_update(self, version: str, url: str, sha256: str) -> None:
        """Ask the device to install a release (normal or lane connection). Progress arrives through `on_update_status`."""
        conn = self._conn or self._lane
        if conn is None:
            raise DeviceOffline("the device is offline")
        try:
            await conn.send(m.encode(m.update(version, url, sha256)))
        except (ConnectionClosed, OSError):
            raise DeviceOffline("the device is offline") from None

    async def command(self, name: str, data: dict | None = None) -> dict:
        """Run a command on the device. Raises DeviceOffline at once when not connected, CommandError on a device error."""
        conn = self._conn
        if conn is None:
            raise DeviceOffline("the device is offline")
        msg_id, self._next_id = self._next_id, self._next_id + 1
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await conn.send(m.encode(m.cmd(msg_id, name, data)))
            return await asyncio.wait_for(future, self._command_timeout)
        except (ConnectionClosed, OSError):
            raise DeviceOffline("the device is offline") from None
        except asyncio.TimeoutError:
            raise CommandError(m.INTERNAL, "the device did not answer in time") from None
        finally:
            self._pending.pop(msg_id, None)

    def write_input(self, events: list[dict]) -> bool:
        """Queue remote input for the device and return at once (False when not connected: nothing is queued).

        Events must already be validated (`m.input_events`). A separate sender task writes them as `input` frames, so input
        is never queued behind an awaited command; while the socket is slow, adjacent moves and scrolls are summed."""
        if self._conn is None or self._stopped:
            return False
        self._outbox.add(events)
        if self._input_task is None or self._input_task.done():
            self._input_task = asyncio.create_task(self._input_loop())
        self._input_wake.set()
        return True

    def send_release(self) -> bool:
        """Tell the device to let go of every held button and key and hide its cursor."""
        return self.write_input([{"k": "release"}])

    @property
    def input_dropped(self) -> int:
        return self._outbox.dropped

    async def _input_loop(self) -> None:
        while True:
            while not len(self._outbox):
                self._input_wake.clear()
                await self._input_wake.wait()
            conn = self._conn
            if conn is None:
                self._outbox.clear()
                continue
            frame = m.input_frame(self._input_seq, self._outbox.take())
            self._input_seq += 1
            try:
                await conn.send(m.encode(frame))
                self.input_frames += 1
            except (ConnectionClosed, OSError):
                self._outbox.clear()  # the read loop notices the loss; stale pointer motion is worthless anyway

    def _reset_input(self) -> None:
        """A connection began or ended: stale input must not reach a new session, and the sequence starts again."""
        self._outbox.clear()
        self._input_seq = 0

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DeviceOffline("the device went offline"))
        self._pending.clear()

    def _probe_delay(self, lost_at: float) -> float:
        return PROBE_FAST_SECONDS if self._clock() - lost_at < PROBE_FAST_WINDOW else PROBE_SLOW_SECONDS

    async def _wait(self, delay: float) -> None:
        """Wait for the next attempt: the delay, or a wake() whichever comes first (a wake kept from during the last attempt skips it)."""
        if self._wake.is_set():
            self._wake.clear()
            return
        sleeper = asyncio.ensure_future(self._sleep(delay))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()
        self._wake.clear()

    async def _run(self) -> None:
        lost_at = self._lost_at = self._clock()  # the fast probing window starts when we start or lose the device
        while not self._stopped:
            delay = None
            self._attempting = True
            try:
                session = await open_session_ex(self._transport, self.host, self.port, self._device_id, self._secret,
                                                self._own, PROBE_CONNECT_TIMEOUT)
                conn = session.conn
                self.failed_attempts = 0
                self._on_hello(session.hello, session.skew)
                if session.skew is not None:
                    self._lane = conn
                    self._on_problem(VersionSkew(session.skew, updatable=True))
                    try:
                        await self._lane_loop(conn)
                    except (ConnectionClosed, OSError, asyncio.TimeoutError):
                        pass
                    # a restart for an update is expected: come back quickly; otherwise do not hammer the device
                    delay = PROBE_FAST_SECONDS if self._update_running else VERSION_SKEW_DELAY
                else:
                    self._conn = conn
                    self._reset_input()
                    self._boot_id, self._last_seq = None, {}
                    self._on_connection(True)
                    await self._read_loop(conn)
            except asyncio.CancelledError:
                raise
            except AuthFailed as e:
                self._on_problem(e)
                return  # a wrong secret will not fix itself: wait for re-pairing
            except VersionSkew as e:
                self._on_problem(e)
                delay = VERSION_SKEW_DELAY  # an update fixes this; do not hammer the device
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                _LOGGER.debug("connection to %s:%s failed: %s", self.host, self.port, e)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error in the device connection")
            finally:
                self._attempting = False
                if self._lane is not None:
                    await self._lane.close()
                    self._lane = None
                    self._lost_at = lost_at = self._clock()
                    self._on_lane_closed()
                if self._conn is not None:
                    await self._conn.close()
                    self._conn = None
                    self._reset_input()
                    lost_at = self._lost_at = self._clock()
                    self._on_connection(False)
                else:
                    self.failed_attempts += 1
                self._fail_pending()
            if self._resolver is not None and self._clock() - self._last_resolve >= RESOLVE_INTERVAL:
                self._last_resolve = self._clock()
                try:
                    found = await self._resolver()
                except Exception:  # noqa: BLE001
                    found = None
                if found:
                    self.set_address(*found)
            await self._wait(delay if delay is not None else self._probe_delay(lost_at))

    def _apply_state(self, frame: dict) -> None:
        """Deliver a state frame unless it is stale; a gap in a group's seq is applied, then repaired by one resync.

        The first frame of a group on a connection (the snapshot) and any frame of a new boot_id are taken as they come."""
        name, boot_id, seq = frame["name"], frame["boot_id"], frame["seq"]
        if boot_id != self._boot_id:
            self._boot_id, self._last_seq = boot_id, {}
        last = self._last_seq.get(name)
        if last is not None and seq <= last:
            _LOGGER.debug("dropping stale %s frame (seq %s, have %s)", name, seq, last)
            return
        gap = last is not None and seq != last + 1
        self._last_seq[name] = seq
        self._on_state(name, frame["data"])
        if gap:
            _LOGGER.debug("gap in %s (seq %s after %s): requesting a resync", name, seq, last)
            self._request_resync()

    def _request_resync(self) -> None:
        now = self._clock()
        if (self._resync_task is not None and not self._resync_task.done()) or now - self._last_resync < RESYNC_MIN_INTERVAL:
            return
        self._last_resync = now
        self._resync_task = asyncio.get_running_loop().create_task(self._resync())

    async def _resync(self) -> None:
        try:
            await self.command("resync")
        except (DeviceOffline, CommandError) as e:
            _LOGGER.debug("resync failed: %s", e)

    _update_running = False  # the last update_status says an install is under way (then a closed lane is expected)

    def _note_update_status(self, frame: dict) -> None:
        self._update_running = frame["state"] in ("downloading", "waiting_for_ui", "installing")
        self._on_update_status(frame)

    async def _lane_loop(self, conn: Connection) -> None:
        """A skewed connection: read update_status frames until the device closes it."""
        while True:
            frame = await _recv_frame(conn, None)
            if frame["type"] == "update_status":
                self._note_update_status(frame)

    async def _read_loop(self, conn: Connection) -> None:
        while True:
            frame = await _recv_frame(conn, None)
            kind = frame["type"]
            if kind == "state":
                self._apply_state(frame)
            elif kind == "update_status":
                self._note_update_status(frame)
            elif kind == "event":
                self._on_event(frame["name"], frame["data"])
            elif kind == "result":
                future = self._pending.get(frame["id"])
                if future is not None and not future.done():
                    if frame["ok"]:
                        future.set_result(frame.get("data", {}))
                    else:
                        future.set_exception(CommandError(frame["error"]["code"], frame["error"].get("message", "")))
