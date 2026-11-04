"""One admitted Sendspin connection, seen from the player (the Noise responder).

Flow (verified against aiosendspin's client): we send `client/init`, the server answers `server/init`, the server sends
Noise message 1, we answer with message 2; from then on every message is encrypted. `server/hello` -> `client/hello` ->
`server/activate`; then time-sync bursts, `client/state`, and the stream. A separate playout task feeds the audio sink so a
slow write never blocks reading. See swe/specs/architecture/sendspin-client.md.

`run()` returns why the session ended. All timing uses injectable clocks so tests need no real audio."""
import asyncio
import time
from typing import Callable

from .. import wsproto
from . import clock as clk
from . import messages as sm
from . import noise
from .crypto import x25519_public
from .scheduler import Drop, Flush, Scheduler, Silence, Write
from .sink import Sink, SinkError

HANDSHAKE_TIMEOUT_S = 10
ACTIVATE_TIMEOUT_S = 30  # the spec drops a provisional connection that has not been activated by then
IDLE_TIMEOUT_S = 30
TIME_REPLY_TIMEOUT_S = 2
BURST_INTERVAL_S = 5  # the spec allows up to 10
INITIAL_BURSTS = 2  # the filter needs two samples before it can be trusted
MAX_FAILED_BURSTS = 3
BUFFER_CAPACITY = 4_000_000
REQUIRED_LEAD_TIME_MS = 250  # TODO(verify) with Music Assistant
PLAYER_ROLE = "player@v1"
POLL_INTERVAL_S = 0.01


def monotonic_us() -> int:
    return time.monotonic_ns() // 1000


class SessionEnded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SendspinSession:
    def __init__(self, reader, writer, *, private_key: bytes, name: str, sink: Sink, volume: int = 50, muted: bool = False,
                 output_delay_ms: int = 0, on_status: Callable | None = None, on_settings: Callable | None = None,
                 now_us: Callable[[], int] = monotonic_us, lookahead_us: int = 100_000, tolerance_us: int = 5_000,
                 burst_interval_s: float = BURST_INTERVAL_S, log: Callable[[str], None] = lambda m: None,
                 admit: Callable | None = None):
        self._reader, self._writer = reader, writer
        self._private_key = private_key
        self._name = name
        self.sink = sink
        self.volume, self.muted, self.output_delay_ms = volume, muted, output_delay_ms
        self._on_status = on_status or (lambda status, **info: None)
        self._on_settings = on_settings or (lambda **changes: None)
        self._now = now_us
        self._burst_interval = burst_interval_s
        self._log = log
        self._admit = admit  # async (session) -> None to accept or a goodbye reason to reject (set by the listener)
        self.client_id = sm.client_id_for(x25519_public(private_key))
        self.server_id = None
        self.server_name = None
        self.activities = []
        self.active_roles = []
        self.unpaired = True
        self.clock = clk.ClockFilter()
        self.scheduler = Scheduler(self.clock.compute_client_time, now_us, latency_us=sink.latency_us, lookahead_us=lookahead_us,
                                   tolerance_us=tolerance_us, capacity_bytes=BUFFER_CAPACITY)
        self.scheduler.output_delay_us = output_delay_ms * 1000
        self.streaming = False
        self.format = None
        self.dropped_frames = 0
        self.ended = None
        self._transport = None
        self._defrag = sm.Defragmenter()
        self._send_lock = asyncio.Lock()
        self._pending_time = None
        self._pending_time_t1 = None
        self._available = False
        self._tasks = []
        self._stop = asyncio.Event()
        self._goodbye = None

    # --- public ----------------------------------------------------------------------------------------------------------------

    @property
    def player_active(self) -> bool:
        return PLAYER_ROLE in self.active_roles

    def request_stop(self, reason: str = "shutdown") -> None:
        """Ask the session to say goodbye and end (the task running `run()` finishes it)."""
        self._goodbye = reason
        self._stop.set()

    async def run(self) -> str:
        """Drive the connection until it ends; returns a short reason. Never raises for peer or network trouble."""
        try:
            await self._handshake()
            await self._exchange_hellos()
            await self._steady()
        except SessionEnded as e:
            self.ended = e.reason
        except wsproto.WsClosed as e:
            self.ended = f"closed ({e.code})"
        except (sm.SendspinProtocolError, noise.NoiseError) as e:
            self.ended = f"protocol error: {e}"
        except asyncio.TimeoutError:
            self.ended = "timed out"
        except asyncio.CancelledError:
            self.ended = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001 - a bug in one session must not take the plugin down
            self._log(f"sendspin session crashed: {e!r}")
            self.ended = f"internal error: {e}"
        finally:
            await self._finish()
        return self.ended

    # --- handshake -------------------------------------------------------------------------------------------------------------

    async def _recv_text(self, timeout: float) -> str:
        while True:
            kind, data = await asyncio.wait_for(wsproto.read_frame(self._reader, self._writer, max_size=65535 + 1024), timeout)
            if kind == wsproto.OP_TEXT:
                return data.decode("utf-8")
            if kind != wsproto.OP_PONG:
                raise sm.SendspinProtocolError("expected a text frame during the handshake")

    async def _handshake(self) -> None:
        client_init = sm.build_client_init(self.client_id)
        await wsproto.send_text(self._writer, client_init)
        server_init = await self._recv_text(HANDSHAKE_TIMEOUT_S)
        self.server_id = sm.parse_server_init(server_init)
        responder = noise.NoiseResponder(self._private_key, sm.public_key_from_id(self.server_id), sm.prologue(client_init, server_init),
                                         allow_unpaired=True)
        message_1 = sm.parse_noise_handshake(await self._recv_text(HANDSHAKE_TIMEOUT_S))
        responder.read_message_1(message_1)
        self.unpaired = responder.unpaired
        message_2, self._transport = responder.write_message_2()
        await wsproto.send_text(self._writer, sm.build_noise_handshake(message_2))

    async def _send(self, plaintext: bytes) -> None:
        async with self._send_lock:
            await wsproto.send_binary(self._writer, self._transport.send(plaintext))

    async def _recv(self, timeout: float) -> tuple:
        """Next message as (kind, value, received_at_us). Pongs and unreadable binary types are skipped by the caller."""
        while True:
            kind, data = await asyncio.wait_for(wsproto.read_frame(self._reader, self._writer, max_size=65535 + 1024), timeout)
            received = self._now()
            if kind == wsproto.OP_PONG:
                continue
            if kind != wsproto.OP_BIN:
                raise sm.SendspinProtocolError("expected binary frames after the handshake")
            whole = self._defrag.feed(self._transport.receive(data))
            if whole is None:
                continue
            message_type, value = sm.parse_server_message(whole)
            return message_type, value, received

    async def _exchange_hellos(self) -> None:
        message_type, payload, _ = await self._recv(HANDSHAKE_TIMEOUT_S)
        if message_type != "server/hello":
            raise sm.SendspinProtocolError(f"expected server/hello, got {message_type}")
        self.server_name = payload["name"]
        await self._send(sm.build_client_hello(self._name, BUFFER_CAPACITY, unpaired_access=True,
                                               device_info={"product_name": "Piston", "manufacturer": "SteamOS"}))
        message_type, payload, _ = await self._recv(ACTIVATE_TIMEOUT_S)
        if message_type != "server/activate":
            raise sm.SendspinProtocolError(f"expected server/activate, got {message_type}")
        await self._apply_activate(payload)

    # --- steady state ----------------------------------------------------------------------------------------------------------

    async def _apply_activate(self, payload: dict) -> None:
        activities = payload["activities"]
        if activities and "playback" not in activities:  # a pairing-only activation: pairing is not supported (deferred)
            await self._say_goodbye("unauthorized")
            raise SessionEnded("pairing is not supported")
        previous_activities = self.activities
        self.activities = activities
        if self._admit is not None:
            reason = await self._admit(self)
            if reason is not None:
                self.activities = previous_activities
                await self._say_goodbye(reason)
                raise SessionEnded(f"rejected ({reason})")
        roles = payload.get("active_roles")
        if roles is not None:
            self.active_roles = [r for r in roles if r == PLAYER_ROLE]
        had_player = self.player_active
        if not self.player_active and self.streaming:
            await self._end_stream()
        if self.player_active and not had_player:
            await self._send_state()
        if not self.streaming:
            self._on_status("connected", server_name=self.server_name)

    async def _steady(self) -> None:
        await self.sink.set_volume(self.volume)
        await self.sink.set_mute(self.muted)
        self._tasks = [asyncio.create_task(self._time_loop()), asyncio.create_task(self._playout_loop())]
        if self.player_active:
            await self._send_state()
        reader = asyncio.create_task(self._read_loop())
        stopper = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait([reader, stopper, *self._tasks], return_when=asyncio.FIRST_COMPLETED)
            failures = [task.exception() for task in done if task in (reader, *self._tasks) and not task.cancelled() and task.exception()]
        finally:
            for task in (reader, stopper):
                task.cancel()
            await asyncio.gather(reader, stopper, return_exceptions=True)  # also retrieves what they ended with
        if self._stop.is_set():
            await self._say_goodbye(self._goodbye or "shutdown")
            raise SessionEnded(f"stopped ({self._goodbye})")
        if failures:
            raise failures[0]  # the reason the session ended

    async def _read_loop(self) -> None:
        while True:
            message_type, value, received = await self._recv(IDLE_TIMEOUT_S)
            await self._handle(message_type, value, received)

    async def _handle(self, message_type: str, value, received_us: int) -> None:
        if message_type == "server/time":
            if self._pending_time is not None and not self._pending_time.done() and value["client_transmitted"] == self._pending_time_t1:
                self._pending_time.set_result((value, received_us))
        elif message_type == "audio":
            if self.streaming and self.clock.converged:
                if not self.scheduler.submit(value.timestamp_us, value.pcm):
                    self.dropped_frames += len(value.pcm) // self.scheduler.frame_bytes
        elif message_type == "stream/start":
            if "player" in value and self.player_active:
                await self._start_stream(value["player"])
        elif message_type == "stream/clear":
            roles = value.get("roles")
            if (roles is None or "player" in roles) and self.streaming:
                await self._apply_actions(self.scheduler.clear(value.get("from_timestamp")))
        elif message_type == "stream/end":
            roles = value.get("roles")
            if (roles is None or "player" in roles) and self.streaming:
                await self._end_stream()
        elif message_type == "server/command":
            if "player" in value and self.player_active:
                await self._player_command(value["player"])
        elif message_type == "server/activate":
            await self._apply_activate(value)
        # group/update, server/state and anything else do not concern a player without metadata

    async def _start_stream(self, fmt: dict) -> None:
        self.scheduler.set_format(fmt["sample_rate"], fmt["channels"] * fmt["bit_depth"] // 8)
        try:
            await self.sink.open(fmt["sample_rate"], fmt["channels"], fmt["bit_depth"])
        except SinkError as e:
            self.streaming = False
            self._log(f"sendspin: audio output failed: {e}")
            self._on_status("error", server_name=self.server_name, error=str(e))
            return
        self.streaming, self.format = True, fmt
        self._on_status("streaming", server_name=self.server_name)
        await self._send_state()

    async def _end_stream(self) -> None:
        self.streaming, self.format = False, None
        self.scheduler.clear()
        await self.sink.close()
        self._on_status("connected", server_name=self.server_name)
        if self.player_active:
            await self._send_state()

    async def _player_command(self, command: dict) -> None:
        name = command["command"]
        changes = {}
        if name == "volume":
            self.volume = command["volume"]
            await self.sink.set_volume(self.volume)
            changes["volume"] = self.volume
        elif name == "mute":
            self.muted = command["mute"]
            await self.sink.set_mute(self.muted)
            changes["muted"] = self.muted
        else:
            self.output_delay_ms = command["output_delay_ms"]
            self.scheduler.output_delay_us = self.output_delay_ms * 1000
            changes["output_delay_ms"] = self.output_delay_ms
        self._on_settings(**changes)
        await self._send_state()

    async def _send_state(self) -> None:
        self._available = self.clock.converged
        await self._send(sm.build_client_state(self._available, self.volume, self.muted, self.output_delay_ms,
                                               required_lead_time_ms=REQUIRED_LEAD_TIME_MS, fmt=self.format))

    # --- clock -----------------------------------------------------------------------------------------------------------------

    async def _time_burst(self) -> bool:
        """One burst of clk.BURST_MESSAGES exchanges; feeds the filter the best sample. False when none came back."""
        samples = []
        for _ in range(clk.BURST_MESSAGES):
            t1 = self._now()
            self._pending_time = asyncio.get_running_loop().create_future()
            self._pending_time_t1 = t1
            await self._send(sm.build_client_time(t1))
            try:
                reply, t4 = await asyncio.wait_for(self._pending_time, TIME_REPLY_TIMEOUT_S)
            except asyncio.TimeoutError:
                continue
            finally:
                self._pending_time = None
            samples.append(clk.sample_from_exchange(t1, reply["server_received"], reply["server_transmitted"], t4))
        best = clk.best_of_burst(samples)
        if best is None:
            return False
        self.clock.update(*best)
        return True

    async def _time_loop(self) -> None:
        failed = 0
        bursts = 0
        while True:
            if await self._time_burst():
                failed = 0
            else:
                failed += 1
                if failed >= MAX_FAILED_BURSTS:
                    raise SessionEnded("the server stopped answering time requests")
            bursts += 1
            if self.player_active and self.clock.converged and not self._available:
                await self._send_state()
            if bursts >= INITIAL_BURSTS:
                await asyncio.sleep(self._burst_interval)

    # --- playout ---------------------------------------------------------------------------------------------------------------

    async def _apply_actions(self, actions: list) -> None:
        for action in actions:
            if isinstance(action, Silence):
                await self.sink.write_silence(action.frames)
            elif isinstance(action, Write):
                await self.sink.write(action.pcm)
            elif isinstance(action, Drop):
                self.dropped_frames += action.frames
            elif isinstance(action, Flush):
                await self.sink.flush()

    async def _playout_loop(self) -> None:
        while True:
            if self.streaming:
                try:
                    await self._apply_actions(self.scheduler.poll())
                except SinkError as e:
                    self._log(f"sendspin: audio output error: {e}")
                    self._on_status("error", server_name=self.server_name, error=str(e))
                    await asyncio.sleep(1)
            due = self.scheduler.next_due_in_us() if self.streaming else None
            await asyncio.sleep(POLL_INTERVAL_S if due is None else min(max(due / 1e6, 0.002), POLL_INTERVAL_S))

    # --- ending ----------------------------------------------------------------------------------------------------------------

    async def _say_goodbye(self, reason: str) -> None:
        if self._transport is None:
            return
        try:
            await asyncio.wait_for(self._send(sm.build_client_goodbye(reason)), 2)
        except (OSError, asyncio.TimeoutError, wsproto.WsClosed):
            pass

    async def _finish(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - already reported through `ended`
                pass
        self.streaming = False
        self.scheduler.clear()
        try:
            await self.sink.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await wsproto.send_close(self._writer, 1000)
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass
        self._on_status("waiting", server_name=None)
