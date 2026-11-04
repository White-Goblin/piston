"""The audio output of the Sendspin player: one `pw-cat` playback stream on the default sink, with its own volume and mute.

Why a stream of its own (swe/notes/sendspin/spike-results.md): it is an ordinary PipeWire stream, so it mixes with game,
app and system audio, follows the default output (HDMI, headphones, CEC) and has a volume independent of the system
volume. Measured on the device: `--raw` is needed for headerless PCM, and the `--volume` flag does not show up in the
stream, so the level is applied through `pactl` once the stream exists. To keep a stream from starting loud, `open()`
feeds it silence and returns only after the level and mute are applied.

Important (found on the device): `pw-cat` reads stdin on its realtime data thread, and `pactl set-sink-input-*` needs that
thread to answer. While the pipe is empty the thread is blocked in read() and the command hangs until audio arrives, so
while the stream is opening (and, via the scheduler's keepalive, whenever nothing is due) silence keeps flowing.

The `Sink` interface is what the session uses; `FakeSink` records calls for tests."""
import asyncio
import json
import os
import time

LATENCY_US = 50_000  # requested stream latency; the scheduler assumes this much between a write and the sound
NODE_WAIT_S = 6.0  # generous: the first start after a wake or a plugin restart can be slow (seen on the device)
NODE_POLL_S = 0.05
PACTL_TIMEOUT_S = 8.0
RESTART_BACKOFF_S = (0.5, 1, 2, 5, 10)
RUNTIME_DIR = f"/run/user/{os.getuid()}" if os.getuid() else "/run/user/1000"
SILENCE_CHUNK_MS = 20
SAMPLE_FORMATS = {16: "s16", 32: "s32"}  # sample size in bits -> pw-cat format
FEED_INTERVAL_S = 0.02


class SinkError(Exception):
    """The audio output could not be opened or used."""


def pulse_percent(volume: int) -> int:
    """The PulseAudio percentage for a Sendspin volume (0-100). The spec suggests amplitude (v/100)^1.5; PulseAudio's
    percentage is the cube root of the amplitude, so the percentage is 100 * (v/100)^0.5."""
    if not 0 <= volume <= 100:
        raise ValueError("volume must be 0-100")
    return round(100 * (volume / 100) ** 0.5)


class Sink:
    """What the session needs from an audio output."""

    latency_us = LATENCY_US
    error = None  # a short reason while the output is not working

    @property
    def alive(self) -> bool:
        raise NotImplementedError

    async def open(self, rate: int, channels: int = 2, bits: int = 16) -> None:
        raise NotImplementedError

    async def write(self, pcm: bytes) -> bool:
        """Append audio; False when it was dropped (output down). May wait while the pipe is full (real-time pacing)."""
        raise NotImplementedError

    async def write_silence(self, frames: int) -> bool:
        raise NotImplementedError

    async def flush(self) -> None:
        """Discard what has been written but not yet played."""
        raise NotImplementedError

    async def set_volume(self, volume: int) -> bool:
        raise NotImplementedError

    async def set_mute(self, muted: bool) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class FakeSink(Sink):
    """Records every call; for tests."""

    def __init__(self):
        self.events = []
        self.volume, self.muted = 50, False
        self.opened = False
        self.fail_open = False
        self.fail_open_times = 0  # fail this many opens, then work
        self.accept_writes = True
        self.error = None

    @property
    def alive(self) -> bool:
        return self.opened and self.accept_writes

    @property
    def written(self) -> bytes:
        return b"".join(e[1] for e in self.events if e[0] == "write")

    async def open(self, rate, channels=2, bits=16):
        if self.fail_open or self.fail_open_times > 0:
            self.fail_open_times = max(0, self.fail_open_times - 1)
            raise SinkError("fake open failure")
        self.opened = True
        self.format = (rate, channels, bits)
        self.events.append(("open", rate, channels, bits))

    async def write(self, pcm):
        if not (self.opened and self.accept_writes):
            return False
        self.events.append(("write", bytes(pcm)))
        return True

    async def write_silence(self, frames):
        if not (self.opened and self.accept_writes):
            return False
        self.events.append(("write", bytes(frames * self.format[1] * self.format[2] // 8)))
        return True

    async def flush(self):
        self.events.append(("flush",))

    async def set_volume(self, volume):
        self.volume = volume
        self.events.append(("volume", volume))
        return True

    async def set_mute(self, muted):
        self.muted = muted
        self.events.append(("mute", muted))
        return True

    async def close(self):
        if self.opened:
            self.events.append(("close",))
        self.opened = False


async def _run_pactl(*args: str) -> str:
    env = {**os.environ, "XDG_RUNTIME_DIR": RUNTIME_DIR}
    try:
        proc = await asyncio.create_subprocess_exec("pactl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        out, err = await asyncio.wait_for(proc.communicate(), PACTL_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise SinkError(f"pactl {' '.join(args[:3])} timed out after {PACTL_TIMEOUT_S:g} s") from None
    except OSError as e:
        raise SinkError(f"pactl could not run: {type(e).__name__}: {e}") from None
    if proc.returncode != 0:
        raise SinkError(f"pactl exited with {proc.returncode}: {err.decode(errors='replace').strip()[:200]}")
    return out.decode()


async def _spawn(*args, **kwargs):
    return await asyncio.create_subprocess_exec(*args, **kwargs)


class PipeWireSink(Sink):
    def __init__(self, spawn=_spawn, pactl=_run_pactl, clock=time.monotonic, sleep=asyncio.sleep, volume: int = 50, muted: bool = False,
                 log=lambda message: None):
        self._spawn, self._raw_pactl, self._clock, self._sleep = spawn, pactl, clock, sleep
        self._log = log
        self._volume, self._muted = volume, muted
        self._proc = None
        self._format = None
        self._index = None
        self._node = None
        self._seq = 0
        self._failures = 0
        self._last_start = 0.0
        self._next_restart = 0.0
        self._env = {**os.environ, "XDG_RUNTIME_DIR": RUNTIME_DIR}
        self.error = None

    async def _pactl(self, *args: str) -> str:
        """Run pactl; a call that is slow (over half a second) or fails is logged with its time, to diagnose the device."""
        started = self._clock()
        try:
            out = await self._raw_pactl(*args)
        except SinkError as e:
            self._log(f"sendspin: pactl {' '.join(args[:3])} failed after {self._clock() - started:.2f} s: {e}")
            raise
        if self._clock() - started > 0.5:
            self._log(f"sendspin: pactl {' '.join(args[:3])} was slow: {self._clock() - started:.2f} s")
        return out

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def _frame_bytes(self) -> int:
        return self._format[1] * self._format[2] // 8

    async def open(self, rate: int, channels: int = 2, bits: int = 16) -> None:
        """`bits` is the size of one sample in the stream: 16 (s16) or 32 (s32, which carries 24-bit audio left-justified:
        `pw-cat --format s32` plays it at the right level, measured on the device)."""
        if bits not in SAMPLE_FORMATS or channels not in (1, 2):
            raise SinkError("only 16-bit and 32-bit mono or stereo is supported")
        await self.close()
        self._format = (rate, channels, bits)
        self._failures = 0
        await self._start()

    async def _start(self) -> None:
        rate, channels, bits = self._format
        self._seq += 1
        self._node = f"piston-sendspin-{self._seq}"
        self._index = None
        try:
            self._proc = await self._spawn(
                "pw-cat", "--playback", "--raw", "--format", SAMPLE_FORMATS[bits], "--rate", str(rate), "--channels", str(channels),
                "--latency", f"{self.latency_us // 1000}ms",
                "-P", f"node.name={self._node} media.name=Piston-Sendspin application.name=Piston", "-",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=self._env)
        except OSError as e:
            self._proc = None
            self.error = f"could not start pw-cat: {e}"
            raise SinkError(self.error) from None
        self._last_start = self._clock()
        try:
            self._proc.stdin.write(bytes(self._frame_bytes * rate * SILENCE_CHUNK_MS // 1000))  # the stream appears with its first data
        except OSError:
            pass  # a process that died at once is reported by _find_stream
        feeder = asyncio.create_task(self._feed_silence(self._proc))  # see the module note: pactl needs audio to be flowing
        try:
            await self._find_stream()
            await self._apply()
        except (SinkError, OSError) as e:
            reason = str(e) if isinstance(e, SinkError) else await self._exit_reason() if not self.alive else str(e)
            await self._stop_feeder(feeder)
            await self._kill()
            self.error = reason or "audio stream did not start"
            raise SinkError(self.error) from None
        await self._stop_feeder(feeder)
        self.error = None

    async def _feed_silence(self, proc) -> None:
        """Real-time silence into a stream that is being set up; ends when cancelled or the pipe breaks."""
        chunk = bytes(self._frame_bytes * self._format[0] * SILENCE_CHUNK_MS // 1000)
        try:
            while proc.returncode is None:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
                await self._sleep(FEED_INTERVAL_S)
                await asyncio.sleep(0)  # always let other tasks run, whatever the injected sleep does
        except (OSError, asyncio.CancelledError):
            pass

    @staticmethod
    async def _stop_feeder(feeder) -> None:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)

    async def _find_stream(self) -> None:
        deadline = self._clock() + NODE_WAIT_S
        while True:
            if not self.alive:
                raise SinkError(await self._exit_reason())
            try:
                for item in json.loads(await self._pactl("-f", "json", "list", "sink-inputs")):
                    if item.get("properties", {}).get("node.name") == self._node:
                        self._index = item["index"]
                        return
            except (ValueError, KeyError, TypeError):
                raise SinkError("unexpected pactl output") from None
            if self._clock() >= deadline:
                raise SinkError("the audio stream did not appear")
            await self._sleep(NODE_POLL_S)
            await asyncio.sleep(0)  # let the silence feeder run whatever the injected sleep does

    async def _apply(self) -> None:
        await self._pactl("set-sink-input-volume", str(self._index), f"{pulse_percent(self._volume)}%")
        await self._pactl("set-sink-input-mute", str(self._index), "1" if self._muted else "0")

    async def _exit_reason(self) -> str:
        proc = self._proc
        try:
            err = (await asyncio.wait_for(proc.stderr.read(300), 0.2)).decode(errors="replace").strip() if proc and proc.stderr else ""
        except (asyncio.TimeoutError, OSError):
            err = ""
        return f"pw-cat exited{f': {err}' if err else ''}"

    async def _kill(self) -> None:
        proc, self._proc, self._index = self._proc, None, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except OSError:
            pass
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 1.0)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass

    async def close(self) -> None:
        self._format = None  # an explicit close: later writes are dropped, never restarted
        await self._kill()

    async def flush(self) -> None:
        """Start a fresh stream with the same format (the only way to discard what is queued in the pipe and in PipeWire)."""
        if self._format is None:
            return
        await self._kill()
        await self._start()

    async def _restart_if_due(self) -> bool:
        if self._format is None or self._clock() < self._next_restart:
            return False
        delay = RESTART_BACKOFF_S[min(self._failures, len(RESTART_BACKOFF_S) - 1)]
        self._failures += 1
        self._next_restart = self._clock() + delay
        await self._kill()
        try:
            await self._start()
        except SinkError:
            return False
        return True

    async def write(self, pcm: bytes) -> bool:
        if self._format is None:
            return False
        if not self.alive:
            if self.error is None:
                self.error = await self._exit_reason() if self._proc is not None else "audio output stopped"
            if not await self._restart_if_due():
                return False
        try:
            self._proc.stdin.write(bytes(pcm))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.error = "audio output closed"
            return False
        if self._failures and self._clock() - self._last_start > 10:
            self._failures = 0
        return True

    async def write_silence(self, frames: int) -> bool:
        if self._format is None:
            return False
        chunk_frames = self._format[0] * SILENCE_CHUNK_MS // 1000
        while frames > 0:
            n = min(frames, chunk_frames)
            if not await self.write(bytes(n * self._frame_bytes)):
                return False
            frames -= n
        return True

    async def set_volume(self, volume: int) -> bool:
        pulse_percent(volume)  # validates
        self._volume = volume
        return await self._reapply(volume=True)

    async def set_mute(self, muted: bool) -> bool:
        self._muted = bool(muted)
        return await self._reapply(volume=False)

    async def _apply_one(self, volume: bool) -> None:
        if volume:
            await self._pactl("set-sink-input-volume", str(self._index), f"{pulse_percent(self._volume)}%")
        else:
            await self._pactl("set-sink-input-mute", str(self._index), "1" if self._muted else "0")

    async def _reapply(self, volume: bool) -> bool:
        """Apply the changed value to the running stream (a stream that is not up yet gets both when it starts)."""
        if self._index is None or not self.alive:
            return True
        try:
            await self._apply_one(volume)
            return True
        except SinkError:
            try:  # the stream may have been re-created under another index
                await self._find_stream()
                await self._apply_one(volume)
                return True
            except SinkError as e:
                self.error = str(e)
                return False
