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
from . import codecs
from . import messages as sm
from . import noise
from .artwork import ARTWORK_TYPE, ArtworkAssembler
from .card import CardState, changed as card_changed
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
METADATA_ROLE, ARTWORK_ROLE, CONTROLLER_ROLE = "metadata@v1", "artwork@v1", "controller@v1"
CARD_PUSH_INTERVAL_S = 0.25  # the card goes to the UI at most four times a second
POLL_INTERVAL_S = 0.01
DECODE_FAILURE_LIMIT = 20  # decode failures within DECODE_FAILURE_WINDOW_S end the stream
DECODE_FAILURE_WINDOW_S = 5.0
OPEN_TOTAL_S = 10.0  # one attempt to open the audio output, whatever it does (the sink has its own, shorter budget)
OPEN_BACKOFF_S = (0.5, 2, 5, 10, 20, 30)  # between attempts while the output does not come up; the stream stays accepted meanwhile
LEVEL_RETRY_S = 2.0   # a volume or mute change that could not be applied is tried again
CLOSE_BUDGET_S = 2.5  # closing the output at the end of a stream or session
LEVELS_BUDGET_S = 5.0  # applying a volume or mute change
WRITE_STALL_S = 2.0   # an output that takes no audio for this long is treated as failed (a stalled pipe would block the audio task for good)


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
                 admit: Callable | None = None, available=frozenset(), monotonic: Callable[[], float] = time.monotonic,
                 on_card: Callable | None = None, wall_clock: Callable[[], float] = time.time):
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
        self.available = frozenset(available)  # the codecs this device can decode ("flac", "opus"); pcm always
        self._monotonic = monotonic
        self._wall = wall_clock
        self._on_card = on_card or (lambda snapshot: None)
        self.card = CardState()
        self.card.set_volume(volume, muted)
        self.artwork = ArtworkAssembler(lambda t: self.clock.compute_client_time(t) if self.clock.converged else None, now_us)
        self._card_sent = None      # the last snapshot given to on_card
        self._card_sent_at = None   # monotonic time of that
        self._card_dirty = False
        self._decoder = None
        # The audio output is only ever touched by the audio task (`_audio_loop`), never by the task that reads the server's
        # messages: a hung audio server must not make the player deaf to the server (swe/notes/sendspin/live-results.md, incident).
        self._want_fmt = None       # (rate, channels, bits, generation) the sink should be open for; None = closed
        self._want_gen = 0
        self._sink_fmt = None       # what it is open for now
        self._sink_ready = False
        self._sink_down = False     # an open failed and is being retried: reported to the server as available=false
        self._open_failures = 0
        self._next_open_at = 0.0
        self._flush_wanted = False
        self._levels_dirty = True
        self._levels_retry_at = 0.0
        self._decode_failures = []  # monotonic times of recent decode failures
        self._seen_decode_errors = 0
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

    def _role_active(self, role: str) -> bool:
        return role in self.active_roles

    @property
    def card_commands(self) -> list:
        return self.card.commands() if self._role_active(CONTROLLER_ROLE) else []

    def abort(self, reason: str = "shutdown") -> None:
        """Synchronous end for a plugin that is unloading and cannot count on its event loop: kill the audio output, send the
        goodbye and the close frame if the connection takes them at once, and drop the connection. Never raises."""
        try:
            self.sink.abort()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._transport is not None:
                self._writer.write(wsproto.frame(wsproto.OP_BIN, self._transport.send(sm.build_client_goodbye(reason))))
            wsproto.send_close_now(self._writer, 1000)
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass

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
        await self._send(sm.build_client_hello(self._name, BUFFER_CAPACITY, unpaired_access=True, available=self.available,
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
            self.active_roles = [r for r in roles if r in sm.ROLES]
        had_player = self.player_active
        self.card.set_session(True)
        if roles is not None:
            self._drop_inactive_card_parts()
        if not self.player_active and self.streaming:
            await self._end_stream()
        if self.player_active and not had_player:
            await self._send_state()
        if not self.streaming:
            self._on_status("connected", server_name=self.server_name)

    def _drop_inactive_card_parts(self) -> None:
        """Roles the server stopped (or never) activated leave nothing of theirs on the card; the player is unaffected."""
        if not self._role_active(METADATA_ROLE):
            self.card.apply_metadata({})
        if not self._role_active(CONTROLLER_ROLE):
            self.card.apply_controller({})
        if not self._role_active(ARTWORK_ROLE):
            self.artwork.reset()
            self.card.set_cover(None)
        self._push_card()

    # --- the card (metadata, cover, controller) ---------------------------------------------------------------------------------

    def _card_snapshot(self) -> dict:
        server_now = self.clock.compute_server_time(self._now()) if self.clock.converged else None
        return self.card.snapshot(server_now, self._wall())

    def _push_card(self, force: bool = False) -> None:
        """Hand the card to `on_card` if it changed; at most four times a second (a later change is flushed by the playout loop)."""
        self._card_dirty = True
        self._flush_card(force)

    def _flush_card(self, force: bool = False) -> None:
        if not self._card_dirty:
            return
        now = self._monotonic()
        if not force and self._card_sent_at is not None and now - self._card_sent_at < CARD_PUSH_INTERVAL_S:
            return
        snapshot = self._card_snapshot()
        self._card_dirty = False
        if not force and not card_changed(self._card_sent, snapshot):
            return
        self._card_sent, self._card_sent_at = snapshot, now
        self._on_card(snapshot)

    async def send_command(self, name: str) -> bool:
        """Send a controller command (previous, play, pause, next) if the server offers it. False when it is not sent."""
        if name not in self.card_commands or self._transport is None:
            return False
        try:
            await self._send(sm.build_client_command(name))
        except (OSError, wsproto.WsClosed, sm.SendspinProtocolError):
            return False
        return True

    async def set_player_volume(self, volume: int) -> None:
        """This device's own player volume (the card's slider): the same path as a server volume command."""
        await self._set_volume(max(0, min(100, int(volume))))
        if self.player_active:
            await self._send_state()

    async def _set_volume(self, volume: int) -> None:
        self.volume = volume
        self._levels_dirty = True
        self.card.set_volume(self.volume, self.muted)
        self._on_settings(volume=volume)
        self._push_card()

    async def _steady(self) -> None:
        self._levels_dirty = True  # applied by the audio task
        self._tasks = [asyncio.create_task(self._time_loop()), asyncio.create_task(self._audio_loop())]
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
                if not self.scheduler.submit(value.timestamp_us, value.pcm):  # the chunk is queued as received (encoded)
                    self.dropped_frames += 1
        elif message_type == "stream/start":
            if "player" in value and self.player_active:
                await self._start_stream(value["player"])
        elif message_type == "stream/clear":
            roles = value.get("roles")
            if (roles is None or "player" in roles) and self.streaming:
                if isinstance(self._decoder, codecs.OpusDecoder):
                    self._decoder.reset()  # Opus predicts from the previous packet: after a clear the next one starts fresh
                if any(isinstance(a, Flush) for a in self.scheduler.clear(value.get("from_timestamp"))):
                    self._flush_wanted = True  # the audio task flushes the output
        elif message_type == "stream/end":
            roles = value.get("roles")
            if (roles is None or "player" in roles) and self.streaming:
                await self._end_stream()
        elif message_type == "server/command":
            if "player" in value and self.player_active:
                await self._player_command(value["player"])
        elif message_type == "server/activate":
            await self._apply_activate(value)
        elif message_type == "server/state":
            if "metadata" in value and self._role_active(METADATA_ROLE):
                self.card.apply_metadata(value["metadata"])
            if "controller" in value and self._role_active(CONTROLLER_ROLE):
                self.card.apply_controller(value["controller"])
            self._push_card()
        elif message_type == "binary":
            kind, data = value
            if kind == ARTWORK_TYPE and self._role_active(ARTWORK_ROLE):  # channel 0 only
                if "changed" in self.artwork.feed(bytes([kind]) + data):
                    self._cover_changed()
        # group/update and anything else are not needed

    def _build_decoder(self, fmt: dict):
        """(decoder or None, sample size in bits for the sink) for a validated stream/start format."""
        codec = fmt["codec"]
        if codec == "pcm":
            return None, 16
        if codec not in self.available:
            raise sm.SendspinProtocolError(f"the server chose {codec}, which was not offered")
        try:
            if codec == "flac":
                decoder = codecs.FlacDecoder(fmt["header"], fmt["sample_rate"], fmt["channels"], fmt["bit_depth"])
                return decoder, 16 if decoder.sample_format == "s16" else 32
            return codecs.OpusDecoder(fmt["sample_rate"], fmt["channels"]), 16
        except codecs.CodecError as e:
            raise sm.SendspinProtocolError(f"cannot decode this {codec} stream: {e}") from None

    def _close_decoder(self) -> None:
        decoder, self._decoder = self._decoder, None
        if isinstance(decoder, codecs.OpusDecoder):
            decoder.close()

    async def _start_stream(self, fmt: dict) -> None:
        """Accept a stream. Only bookkeeping happens here (the read loop must stay responsive); the audio task opens the
        output, retrying with a backoff if it does not come up, and sends the state once it plays."""
        checked = sm.validate_stream_format(fmt)
        self._close_decoder()
        decoder, sink_bits = self._build_decoder(checked)
        self._decoder = decoder
        self._decode_failures.clear()
        self.scheduler.set_format(checked["sample_rate"], checked["channels"] * sink_bits // 8, decode=decoder.decode if decoder else None)
        self._seen_decode_errors = 0
        self._log(f"sendspin: stream {checked['codec']} {checked['sample_rate']} Hz {checked['bit_depth'] if checked['codec'] != 'opus' else 16}-bit")
        self.streaming, self.format = True, fmt
        self._want_gen += 1  # a new stream always gets a fresh output stream (nothing of the previous one is left in it)
        self._want_fmt = (checked["sample_rate"], checked["channels"], sink_bits, self._want_gen)
        self._open_failures, self._next_open_at = 0, 0.0

    async def _end_stream(self, error: str | None = None) -> None:
        self.streaming, self.format = False, None
        self._want_fmt, self._sink_down, self._flush_wanted = None, False, False
        self.scheduler.clear()
        self._close_decoder()  # the audio task closes the output
        if error:
            self._on_status("error", server_name=self.server_name, error=error)
        else:
            self._on_status("connected", server_name=self.server_name)
        if self.player_active:
            await self._send_state()

    def _cover_changed(self) -> None:
        cover = self.artwork.current()
        self.card.set_cover(cover.id if cover else None)
        self._push_card()

    async def _player_command(self, command: dict) -> None:
        name = command["command"]
        changes = {}
        if name == "volume":
            self.volume = command["volume"]
            changes["volume"] = self.volume
        elif name == "mute":
            self.muted = command["mute"]
            changes["muted"] = self.muted
        else:
            self.output_delay_ms = command["output_delay_ms"]
            self.scheduler.output_delay_us = self.output_delay_ms * 1000
            changes["output_delay_ms"] = self.output_delay_ms
        self._levels_dirty = True  # the audio task applies it to the output
        self._on_settings(**changes)
        self.card.set_volume(self.volume, self.muted)
        self._push_card()
        await self._send_state()

    async def _send_state(self) -> None:
        self._available = self.clock.converged and not self._sink_down  # an output that does not come up is reported as unavailable
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

    async def _decode_failed_too_often(self) -> bool:
        """DECODE_FAILURE_LIMIT failures within DECODE_FAILURE_WINDOW_S end the stream with status error."""
        new = self.scheduler.decode_errors - self._seen_decode_errors
        if new <= 0:
            return False
        self._seen_decode_errors = self.scheduler.decode_errors
        now = self._monotonic()
        self._decode_failures.extend([now] * new)
        self._decode_failures = [t for t in self._decode_failures if now - t <= DECODE_FAILURE_WINDOW_S]
        if len(self._decode_failures) < DECODE_FAILURE_LIMIT:
            return False
        self._log("sendspin: too many chunks could not be decoded; ending the stream")
        await self._end_stream(error="decode failed")
        return True

    # --- the audio task: the only code that touches the audio output ------------------------------------------------------------

    async def _audio_loop(self) -> None:
        """Reconciles the output with what is wanted (open, level, flush, close) and plays the scheduled audio. Every
        call is bounded, an output that fails is retried with a backoff while the server keeps its connection, and nothing
        here can stop the task that reads the server's messages."""
        last_error_log = 0.0
        while True:
            try:
                if self.artwork.waiting() and self.artwork.poll():
                    self._cover_changed()
                self._flush_card()
                await self._reconcile_sink()
                if self.streaming:
                    if self._sink_ready:
                        try:
                            await asyncio.wait_for(self._playout(), WRITE_STALL_S)
                        except asyncio.TimeoutError:
                            await self._sink_failed("the audio output stopped taking audio", abort=True)
                        except SinkError as e:
                            await self._sink_failed(str(e))
                        if await self._decode_failed_too_often():
                            continue
                    else:
                        self.scheduler.discard_due()  # the output is down: what is due cannot play, the future audio is kept
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a bug or an odd failure here must not end the session
                if self._monotonic() - last_error_log > 30:
                    last_error_log = self._monotonic()
                    self._log(f"sendspin: audio task error: {e!r}")
                await asyncio.sleep(1)
            due = self.scheduler.next_due_in_us() if self.streaming and self._sink_ready else None
            await asyncio.sleep(POLL_INTERVAL_S if due is None else min(max(due / 1e6, 0.002), POLL_INTERVAL_S))

    async def _playout(self) -> None:
        await self._apply_actions(self.scheduler.poll())
        await self._apply_actions(self.scheduler.keepalive())

    async def _reconcile_sink(self) -> None:
        now = self._monotonic()
        if self._levels_dirty and now >= self._levels_retry_at:
            await self._apply_levels(now)
        want = self._want_fmt
        if want is None:
            if self._sink_fmt is not None or self._sink_ready:
                await self._close_sink()
            return
        if self._sink_fmt != want:
            if self._sink_fmt is not None:
                await self._close_sink()
            if now >= self._next_open_at:
                await self._open_sink(want)
            return
        if self._flush_wanted:
            self._flush_wanted = False
            try:
                await asyncio.wait_for(self.sink.flush(), OPEN_TOTAL_S)
            except (SinkError, asyncio.TimeoutError) as e:
                await self._sink_failed(f"flush failed: {e or 'timed out'}", abort=True)

    async def _apply_levels(self, now: float) -> None:
        """Volume and mute to the output. Bounded; a failure is retried later and never ends anything."""
        self._levels_dirty = False
        try:
            ok = await asyncio.wait_for(self._set_levels(), LEVELS_BUDGET_S)
        except (SinkError, asyncio.TimeoutError):
            ok = False
        if not ok:
            self._levels_dirty = True
            self._levels_retry_at = now + LEVEL_RETRY_S
            self._log("sendspin: could not apply the volume to the audio output; trying again")

    async def _set_levels(self) -> bool:
        volume_ok = await self.sink.set_volume(self.volume)
        mute_ok = await self.sink.set_mute(self.muted)
        return bool(volume_ok and mute_ok)

    async def _open_sink(self, want: tuple) -> None:
        try:
            await asyncio.wait_for(self.sink.open(*want[:3]), OPEN_TOTAL_S)
        except (SinkError, asyncio.TimeoutError) as e:
            await self._sink_failed(str(e) or f"opening the output took over {OPEN_TOTAL_S:g} s", abort=True)
            return
        self._sink_fmt, self._sink_ready = want, True
        recovered, self._sink_down = self._sink_down, False
        self._open_failures = 0
        if recovered:
            self._log("sendspin: the audio output is working again")
        if self._want_fmt == want:  # not ended or replaced while it opened
            self._on_status("streaming", server_name=self.server_name)
            if self.player_active:
                await self._send_state()

    async def _sink_failed(self, reason: str, abort: bool = False) -> None:
        """The output does not work: drop it, retry after a backoff, tell the server it is unavailable meanwhile."""
        if abort:
            self.sink.abort()  # leaves no half-open process behind (the sink may be in the middle of something)
        self._sink_ready, self._sink_fmt = False, None
        self._open_failures += 1
        delay = OPEN_BACKOFF_S[min(self._open_failures - 1, len(OPEN_BACKOFF_S) - 1)]
        self._next_open_at = self._monotonic() + delay
        self._log(f"sendspin: audio output failed (attempt {self._open_failures}): {reason}; retrying in {delay:g} s")
        if self._open_failures in (1, 5):
            self._log(f"sendspin: audio output diagnostics: {self.sink.diagnostics()}")
        first = not self._sink_down
        self._sink_down = True
        self._on_status("error", server_name=self.server_name, error=reason)
        if first and self.player_active:
            await self._send_state()  # available=false: the server knows the player cannot play right now

    async def _close_sink(self) -> None:
        self._sink_ready, self._sink_fmt = False, None
        try:
            await asyncio.wait_for(self.sink.close(), CLOSE_BUDGET_S)
        except (SinkError, asyncio.TimeoutError, OSError):
            self._log("sendspin: the audio output did not close in time; stopping it")
            self.sink.abort()

    # --- ending ----------------------------------------------------------------------------------------------------------------

    async def _say_goodbye(self, reason: str) -> None:
        if self._transport is None:
            return
        try:
            await asyncio.wait_for(self._send(sm.build_client_goodbye(reason)), 2)
        except (OSError, asyncio.TimeoutError, wsproto.WsClosed):
            pass

    async def _finish(self) -> None:
        """Tear everything down. The audio output is always released, even when this is itself cancelled (the plugin is being
        unloaded and ran out of time): a `pw-cat` left behind would keep a stream on the audio server."""
        cancelled = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                cancelled = cancelled or not task.cancelled()  # our own cancellation, not the task's
            except Exception:  # noqa: BLE001 - already reported through `ended`
                pass
        self.streaming = False
        self.scheduler.clear()
        self._close_decoder()
        self.artwork.reset()
        self.card.clear()
        if self._card_sent is None or self._card_sent["active"]:
            self._card_dirty = True
            self._flush_card(force=True)  # the final {active: false}
        try:
            await asyncio.wait_for(self.sink.close(), CLOSE_BUDGET_S)
        except asyncio.CancelledError:
            cancelled = True
            self.sink.abort()
        except Exception:  # noqa: BLE001 - includes the timeout: whatever is left of the output is killed at once
            self.sink.abort()
        try:
            await asyncio.wait_for(wsproto.send_close(self._writer, 1000), 1.0)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass
        self._on_status("waiting", server_name=None)
        if cancelled:
            raise asyncio.CancelledError
