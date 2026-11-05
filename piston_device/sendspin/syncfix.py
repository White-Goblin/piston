"""Playback sync correction: keeps the audio hardware in step with the server timeline over a long stream.

The clock sync (`client/time`) makes the device's clock agree with the server's. The audio hardware has a clock of its own that
runs a little faster or slower (typically up to a hundred parts per million, which is 6 ms a minute), so a stream that plays
for twenty minutes (Music Assistant's crossfade makes that normal) slowly drains or fills the buffer between us and the
hardware, until it underruns or the audio lags. The reference player measures the real playback position and trims the
difference with single frames dropped or inserted. We measure it from how full the pipe to `pw-cat` is.

`DriftTracker` is the estimator and the planner (pure logic). `error_us` is how much later than modelled the audio plays, as a
slow average relative to the first seconds of the stream (a constant offset, such as the sink's own latency, does not matter,
only its change). Corrections are small and slow: spread over a window of a minute, at most a tenth of a percent of the audio, and only
once the error is a few milliseconds, so they are inaudible. (Lessons from the device, 2026-11-04: the pipe fill moves in blocks of the stream's period, so the
estimate wanders by about +-12 ms over minutes, and the cushion in the pipe really does drain over about twenty minutes (it
fell from 83 ms to 12 ms, where the first version of this stream stuttered). A loop that dropped frames on noise made the
cushion smaller still. So the loop is lopsided: inserting frames grows the cushion and is done readily, dropping frames only
happens for a large lag.)
"""
import math

INSERT_ENTER_US = 8_000      # audio playing this much EARLIER than modelled (the pipe is draining): insert frames, which only
INSERT_LEAVE_US = 3_000      # grows the cushion, so acting on a noisy reading costs nothing
DROP_ENTER_US = 40_000       # audio playing this much LATER: drop frames, which shrinks the cushion, so only for a real lag
DROP_LEAVE_US = 15_000
WINDOW_S = 60.0              # a correction is planned over this long: gentle, because the measurement is noisy
MAX_FRACTION = 0.001         # never alter more than this share of the audio (1 ms per second)
TAU_S = 45.0                 # time constant of the average: the pipe fill is noisy (it moves in blocks of the stream's period)
WARMUP_S = 15.0              # ignore the start of a stream (the output is still settling: a baseline taken earlier was off by 10 ms)
BASELINE_S = 30.0            # the average over this long after the warmup is the reference
DISCONTINUITY_US = 100_000   # a jump this big is a restart of the output, not drift: start over


class DriftTracker:
    def __init__(self, tau_s: float = TAU_S, warmup_s: float = WARMUP_S, baseline_s: float = BASELINE_S,
                 insert_enter_us: float = INSERT_ENTER_US, insert_leave_us: float = INSERT_LEAVE_US,
                 drop_enter_us: float = DROP_ENTER_US, drop_leave_us: float = DROP_LEAVE_US, window_s: float = WINDOW_S,
                 max_fraction: float = MAX_FRACTION):
        self.tau_s, self.warmup_s, self.baseline_s = tau_s, warmup_s, baseline_s
        self.insert_enter_us, self.insert_leave_us = insert_enter_us, insert_leave_us
        self.drop_enter_us, self.drop_leave_us = drop_enter_us, drop_leave_us
        self.window_s, self.max_fraction = window_s, max_fraction
        self.corrected_frames = 0  # signed running total (positive = dropped), for the log
        self.resets = 0
        self.reset()

    def reset(self) -> None:
        self._t0 = None
        self._last = None
        self._sum, self._count = 0.0, 0
        self._baseline = None
        self._ema = 0.0
        self._correcting = False
        self._carry = 0.0

    @property
    def ready(self) -> bool:
        return self._baseline is not None

    @property
    def error_us(self) -> float:
        """How much later than modelled the audio plays now, relative to the start of the stream (negative = earlier)."""
        return self._ema - self._baseline if self.ready else 0.0

    def observe(self, now_us: float, raw_us: float) -> None:
        """`raw_us` = when the next frame to be written would really play (now + what the pipe holds) minus when the model
        says it plays."""
        if self._t0 is None:
            self._t0 = self._last = now_us
        elapsed = (now_us - self._t0) / 1e6
        if elapsed < self.warmup_s:
            self._last = now_us
            return
        if not self.ready:
            self._sum += raw_us
            self._count += 1
            if elapsed >= self.warmup_s + self.baseline_s and self._count:
                self._baseline = self._ema = self._sum / self._count
            self._last = now_us
            return
        if abs(raw_us - self._ema) > DISCONTINUITY_US:  # the output was restarted: measure again from here
            self.resets += 1
            self.reset()
            return
        dt = max(0.0, (now_us - self._last) / 1e6)
        self._last = now_us
        self._ema += (1 - math.exp(-dt / self.tau_s)) * (raw_us - self._ema)

    def frames_to_correct(self, chunk_frames: int, rate: int) -> int:
        """For the next chunk: how many frames to drop (positive, the audio lags) or insert (negative, it runs ahead)."""
        if not self.ready or chunk_frames <= 0:
            return 0
        error = self.error_us
        enter, leave = (self.insert_enter_us, self.insert_leave_us) if error < 0 else (self.drop_enter_us, self.drop_leave_us)
        if not self._correcting and abs(error) >= enter:
            self._correcting = True
        elif self._correcting and abs(error) <= leave:
            self._correcting = False
            self._carry = 0.0
        if not self._correcting:
            return 0
        chunk_us = chunk_frames * 1e6 / rate
        want_us = error * chunk_us / (self.window_s * 1e6)       # this chunk's share of a plan that fixes it within the window
        cap_us = chunk_us * self.max_fraction
        want_us = max(-cap_us, min(cap_us, want_us))
        total = self._carry + want_us * rate / 1e6
        frames = int(total)                                        # toward zero; the fraction carries to the next chunk
        self._carry = total - frames
        self._ema -= frames * 1e6 / rate                           # the measurement will drop by what we remove
        self.corrected_frames += frames
        return frames


def apply_correction(pcm: bytes, frame_bytes: int, frames: int) -> bytes:
    """Drop `frames` single frames from the chunk (positive) or repeat single frames (negative), evenly spread, so the audio
    changes by well under a millisecond in any one place."""
    total = len(pcm) // frame_bytes
    count = abs(frames)
    if count == 0 or total < 2:
        return pcm
    if frames > 0:
        count = min(count, total - 1)
    step = max(1, total // (count + 1))
    out = bytearray()
    start = 0
    for i in range(1, count + 1):
        cut = min(total, step * i)
        if frames > 0:
            out += pcm[start * frame_bytes:(cut - 1) * frame_bytes]      # the frame at cut-1 is dropped
            start = cut
        else:
            out += pcm[start * frame_bytes:cut * frame_bytes]
            out += pcm[(cut - 1) * frame_bytes:cut * frame_bytes]        # the frame before the cut is repeated
            start = cut
    out += pcm[start * frame_bytes:]
    return bytes(out)
