"""Turns timestamped audio chunks into what to append to the continuous sink stream so that every sample plays at its
server timestamp: the chunks wait in a queue until they are about to play, then become `Silence` (the chunk is early),
`Write` (on time), a trimmed `Write` or a `Drop` (late). Pure logic with injectable clocks; no audio hardware.

The stream model: the sink plays whatever is appended, back to back. `cursor_us` is the local time at which the next
appended frame would play. When nothing has been appended for a while the stream has run dry, and the earliest a new frame
can play is now + the sink's latency."""
from collections import deque
from typing import Callable, NamedTuple

from .syncfix import apply_correction


class Write(NamedTuple):
    at_us: float  # local time the first frame will play
    pcm: bytes


class Silence(NamedTuple):
    frames: int


class Drop(NamedTuple):
    frames: int  # a chunk (or the late front of one) that was too late to play in sync


class Flush(NamedTuple):
    """Discard what the sink still holds (a `stream/clear` without a timestamp)."""


class Scheduler:
    """Chunks wait here as they arrived. With a `decode` callable they are queued ENCODED and decoded when released: the
    server fills the announced buffer_capacity with encoded data (about 21 s of FLAC, 30 s of Opus ahead), and decoding on
    arrival would need several times that in PCM. `queued_bytes` therefore counts what was queued, like the server does."""

    def __init__(self, to_local: Callable[[int], int], now: Callable[[], int], rate: int = 48000, frame_bytes: int = 4,
                 latency_us: int = 50_000, lookahead_us: int = 100_000, tolerance_us: int = 5_000, capacity_bytes: int = 4_000_000,
                 decode: Callable[[bytes], bytes] | None = None, tracker=None):
        self.tracker = tracker  # a DriftTracker: trims single frames so the hardware clock cannot drift away (see syncfix.py)
        self._to_local, self._now = to_local, now
        self.latency_us, self.lookahead_us, self.tolerance_us = latency_us, lookahead_us, tolerance_us
        self.output_delay_us = 0
        self.capacity_bytes = capacity_bytes
        self.rate, self.frame_bytes = rate, frame_bytes
        self.decode = decode
        self.decode_errors = 0  # chunks that could not be decoded (dropped; the gap becomes silence)
        self._queue = deque()
        self._queued_bytes = 0
        self.cursor_us = None

    def set_format(self, rate: int, frame_bytes: int, decode: Callable[[bytes], bytes] | None = None) -> None:
        """A new stream: drop everything and start aligning from scratch. `decode` is the new stream's decoder (None for PCM)."""
        self.rate, self.frame_bytes = rate, frame_bytes
        self.decode = decode
        self.decode_errors = 0
        if self.tracker is not None:
            self.tracker.reset()
        self._queue.clear()
        self._queued_bytes = 0
        self.cursor_us = None

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    def _us(self, frames: int) -> float:
        return frames * 1_000_000 / self.rate

    def submit(self, ts_us: int, pcm: bytes) -> bool:
        """Queue a chunk. False when it was refused (empty, or without a decoder not whole frames, or the server overran the
        announced capacity)."""
        if not pcm or (self.decode is None and len(pcm) % self.frame_bytes):
            return False
        if self._queued_bytes + len(pcm) > self.capacity_bytes:
            return False
        self._queue.append((ts_us, bytes(pcm)))
        self._queued_bytes += len(pcm)
        return True

    def _target(self, ts_us: int) -> float:
        return self._to_local(ts_us) - self.output_delay_us

    def next_due_in_us(self) -> float | None:
        """How long until the oldest queued chunk should be released (0 if due now); None when the queue is empty."""
        if not self._queue:
            return None
        return max(0.0, self._target(self._queue[0][0]) - self.latency_us - self.lookahead_us - self._now())

    def poll(self) -> list:
        """Actions for every chunk that is now due, in order."""
        actions = []
        while self._queue:
            ts, pcm = self._queue[0]
            target = self._target(ts)
            now = self._now()
            if target - self.latency_us - self.lookahead_us > now:
                break
            self._queue.popleft()
            self._queued_bytes -= len(pcm)
            if self.decode is not None:
                try:
                    pcm = self.decode(pcm)
                except Exception:  # noqa: BLE001 - a chunk that cannot be decoded is dropped, whatever the decoder raised
                    pcm = b""
                if not pcm or len(pcm) % self.frame_bytes:
                    self.decode_errors += 1
                    continue  # the next chunk finds the cursor behind its target and fills the gap with silence
            earliest = now + self.latency_us
            cursor = earliest if self.cursor_us is None else max(self.cursor_us, earliest)
            offset = target - cursor
            frames = len(pcm) // self.frame_bytes
            if offset > self.tolerance_us:  # early: wait with silence
                gap = round(offset * self.rate / 1_000_000)
                actions.append(Silence(gap))
                cursor += self._us(gap)
            elif offset < -self.tolerance_us:  # late: skip the part that should already have played
                late = round(-offset * self.rate / 1_000_000)
                if late >= frames:
                    actions.append(Drop(frames))
                    self.cursor_us = cursor
                    continue
                actions.append(Drop(late))
                pcm = pcm[late * self.frame_bytes:]
                frames -= late
            if self.tracker is not None and frames > 1:
                trim = self.tracker.frames_to_correct(frames, self.rate)
                if trim:
                    pcm = apply_correction(pcm, self.frame_bytes, trim)  # the cursor below stays in the server's timeline
            actions.append(Write(cursor, pcm))
            self.cursor_us = cursor + self._us(frames)
        return actions

    def discard_due(self) -> int:
        """Drop the queued chunks that have come due without decoding them (the audio output is down, so they could not be
        played anyway); chunks that are still in the future stay, so audio resumes where the server is when the output
        comes back. Returns how many were dropped."""
        dropped = 0
        while self._queue:
            ts, pcm = self._queue[0]
            if self._target(ts) - self.latency_us - self.lookahead_us > self._now():
                break
            self._queue.popleft()
            self._queued_bytes -= len(pcm)
            dropped += 1
        if dropped:
            self.cursor_us = None
        return dropped

    def observe(self, pending_us: float | None) -> None:
        """What the output holds right now (None when unknown): feeds the drift tracker."""
        if self.tracker is None or pending_us is None or self.cursor_us is None:
            return
        now = self._now()
        self.tracker.observe(now, now + pending_us - self.cursor_us)

    def keepalive(self, min_fill_us: int = 10_000) -> list:
        """Silence that keeps the sink fed while nothing is due, as far ahead as the release window (latency + lookahead)
        but never past the next queued chunk's play time. A starved sink has real consequences: `pw-cat` blocks on an empty
        pipe and then cannot answer volume or mute changes. The silence advances the cursor like any other write."""
        now = self._now()
        earliest = now + self.latency_us
        cursor = earliest if self.cursor_us is None else max(self.cursor_us, earliest)
        limit = now + self.latency_us + self.lookahead_us
        if self._queue:
            limit = min(limit, self._target(self._queue[0][0]))
        if limit - cursor < min_fill_us:
            return []
        frames = round((limit - cursor) * self.rate / 1_000_000)
        if frames <= 0:
            return []
        self.cursor_us = cursor + self._us(frames)
        return [Silence(frames)]

    def clear(self, from_ts_us: int | None = None) -> list:
        """`stream/clear`. Without a timestamp everything goes (queue and cursor) and the sink must be flushed; with one,
        only queued chunks at or after it are discarded (audio already handed to the sink is past recall)."""
        if from_ts_us is None:
            if self.tracker is not None:
                self.tracker.reset()
            self._queue.clear()
            self._queued_bytes = 0
            self.cursor_us = None
            return [Flush()]
        kept = deque(item for item in self._queue if item[0] < from_ts_us)
        self._queued_bytes = sum(len(p) for _, p in kept)
        self._queue = kept
        return []
