import asyncio
import math
import os
import random
import unittest

from piston_device.sendspin.scheduler import Scheduler, Write
from piston_device.sendspin.sink import PipeWireSink
from piston_device.sendspin.syncfix import DriftTracker, apply_correction

RATE = 48000
FB = 8


def frames(n, start=0):
    return b"".join((start + i).to_bytes(4, "little") * 2 for i in range(n))


def values(pcm):
    return [int.from_bytes(pcm[i:i + 4], "little") for i in range(0, len(pcm), FB)]


class ApplyCorrectionTests(unittest.TestCase):
    def test_dropping_removes_single_frames_spread_across_the_chunk(self):
        out = apply_correction(frames(100), FB, 4)
        v = values(out)
        self.assertEqual(len(v), 96)
        self.assertEqual(v, sorted(v))                                  # order kept, nothing duplicated
        gaps = [b - a for a, b in zip(v, v[1:]) if b - a > 1]
        self.assertEqual(len(gaps), 4)                                  # four separate single-frame holes
        self.assertTrue(all(g == 2 for g in gaps))

    def test_inserting_repeats_single_frames(self):
        out = apply_correction(frames(100), FB, -3)
        v = values(out)
        self.assertEqual(len(v), 103)
        self.assertEqual(sum(1 for a, b in zip(v, v[1:]) if a == b), 3)
        self.assertEqual(sorted(set(v)), list(range(100)))              # nothing lost

    def test_nothing_to_do_and_tiny_chunks_are_left_alone(self):
        pcm = frames(50)
        self.assertEqual(apply_correction(pcm, FB, 0), pcm)
        self.assertEqual(apply_correction(frames(1), FB, 5), frames(1))
        self.assertEqual(len(values(apply_correction(frames(3), FB, 50))), 1)   # never removes the whole chunk
        self.assertEqual(len(values(apply_correction(frames(10), FB, 100))), 1)


class TrackerTests(unittest.TestCase):
    def run_sim(self, seconds, drift_ppm=0.0, noise_us=0.0, chunk=4608, seed=1):
        """A world in which the audio plays `drift_ppm` slower than modelled; corrections remove what they drop."""
        rnd = random.Random(seed)
        t = DriftTracker()
        applied_us = 0.0
        now = 0.0
        next_chunk = 0.0
        history = []
        while now < seconds * 1e6:
            now += 10_000
            sawtooth = noise_us * (((now / 1e6) * 0.37) % 1.0 - 0.5) * 2
            raw = 40_000 + drift_ppm * 1e-6 * now - applied_us + sawtooth + rnd.uniform(-2000, 2000) * (noise_us > 0)
            t.observe(now, raw)
            if now >= next_chunk:
                next_chunk += chunk * 1e6 / RATE
                n = t.frames_to_correct(chunk, RATE)
                applied_us += n * 1e6 / RATE
            history.append((now, t.error_us))
        return t, history, applied_us

    def test_no_drift_and_noisy_measurements_cause_no_corrections(self):
        t, _, applied = self.run_sim(300, drift_ppm=0, noise_us=25_000)
        self.assertEqual(t.corrected_frames, 0)
        self.assertEqual(applied, 0)

    def test_hardware_running_slow_is_followed_by_dropping_frames(self):
        t, history, applied = self.run_sim(600, drift_ppm=100)               # 6 ms a minute
        self.assertGreater(t.corrected_frames, 0)
        self.assertGreater(applied / 1e6, 0.02)                              # it removed most of what drifted
        late = [abs(e) for now, e in history if now > 120e6]
        self.assertLess(max(late), 9_000)                                    # a gentle proportional loop settles near drift x window (6 ms)

    def test_hardware_running_fast_is_followed_by_inserting_frames(self):
        t, history, _ = self.run_sim(600, drift_ppm=-100)
        self.assertLess(t.corrected_frames, 0)
        self.assertLess(max(abs(e) for now, e in history if now > 120e6), 9_000)

    def test_drift_with_noise_still_converges(self):
        t, history, _ = self.run_sim(900, drift_ppm=150, noise_us=20_000)
        self.assertGreater(t.corrected_frames, 0)
        self.assertLess(max(abs(e) for now, e in history if now > 200e6), 12_000)

    def test_the_drift_measured_on_the_device_leads_to_almost_no_correction(self):
        """10 parts per million with the noise of the pipe fill (seen on the device): a first version chased the noise and dropped
        16 ms in 15 seconds. Now the total correction over 15 minutes stays close to the real drift (9 ms)."""
        t, history, applied = self.run_sim(900, drift_ppm=10, noise_us=25_000)
        self.assertLess(abs(applied), 30_000)
        self.assertLess(max(abs(e) for now, e in history if now > 120e6), 12_000)
        self.assertLess(abs(t.corrected_frames), 1500)

    def test_corrections_are_small_and_rate_limited(self):
        t = DriftTracker(warmup_s=0, baseline_s=0.01)
        t.observe(0, 0)
        t.observe(20_000, 0)
        t.observe(40_000, 0)                                                 # baseline set at 0
        self.assertTrue(t.ready)
        t._ema = 500_000                                                     # a huge error
        n = t.frames_to_correct(4608, RATE)
        self.assertLessEqual(n, math.ceil(4608 * 0.001))                     # at most a tenth of a percent of the chunk

    def test_a_restart_of_the_output_starts_the_measurement_over(self):
        t = DriftTracker(warmup_s=0, baseline_s=1)
        for i in range(300):
            t.observe(i * 10_000, 1000)
        self.assertTrue(t.ready)
        t.observe(3_100_000, 1000 + 500_000)                                 # the pipe was emptied and refilled
        self.assertFalse(t.ready)
        self.assertEqual(t.resets, 1)
        self.assertEqual(t.frames_to_correct(4608, RATE), 0)

    def test_nothing_happens_before_the_baseline_exists(self):
        t = DriftTracker()
        t.observe(0, 0)
        t.observe(1_000_000, 50_000)
        self.assertFalse(t.ready)
        self.assertEqual(t.frames_to_correct(4608, RATE), 0)


class SchedulerIntegrationTests(unittest.TestCase):
    def test_a_correction_shortens_the_written_chunk_but_not_the_timeline(self):
        t = [10_000_000]
        tracker = DriftTracker(warmup_s=0, baseline_s=0.01)
        s = Scheduler(lambda ts: ts - 1_000_000, lambda: t[0], rate=RATE, frame_bytes=FB, tracker=tracker)
        tracker.observe(0, 0), tracker.observe(20_000, 0), tracker.observe(40_000, 0)
        tracker._ema = 20_000                                                # audio plays 20 ms late: drop some frames
        s.submit(1_000_000 + 10_100_000, frames(4800))                       # 100 ms chunk, due soon
        t[0] = 10_000_000
        actions = [a for a in s.poll() if isinstance(a, Write)]
        written = sum(len(a.pcm) for a in actions) // FB
        self.assertLess(written, 4800)
        self.assertGreaterEqual(written, 4800 - 6)                           # a few frames only (<= 0.1%)
        self.assertAlmostEqual(s.cursor_us, actions[0].at_us + 100_000, delta=10)   # the timeline advanced by the whole chunk

    def test_observe_needs_a_cursor_and_a_known_fill(self):
        tracker = DriftTracker()
        s = Scheduler(lambda ts: ts, lambda: 0, rate=RATE, frame_bytes=FB, tracker=tracker)
        s.observe(10_000)                                                    # no cursor yet
        s.observe(None)
        self.assertIsNone(tracker._t0)

    def test_clear_and_a_new_format_reset_the_tracker(self):
        tracker = DriftTracker(warmup_s=0, baseline_s=0.01)
        s = Scheduler(lambda ts: ts, lambda: 0, rate=RATE, frame_bytes=FB, tracker=tracker)
        tracker.observe(0, 0), tracker.observe(20_000, 0), tracker.observe(40_000, 0)
        self.assertTrue(tracker.ready)
        s.clear()
        self.assertFalse(tracker.ready)
        tracker.observe(0, 0), tracker.observe(20_000, 0), tracker.observe(40_000, 0)
        s.set_format(44100, 4)
        self.assertFalse(tracker.ready)


class PendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_pipe_fill_is_read_from_the_kernel(self):
        read_fd, write_fd = os.pipe()
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, os.fdopen(write_fd, "wb"))
        writer = asyncio.StreamWriter(transport, protocol, None, loop)
        sink = PipeWireSink()
        sink._proc = type("P", (), {"stdin": writer, "returncode": None})()
        sink._format = (RATE, 2, 32)
        try:
            writer.write(bytes(48000 * 8 // 10))                             # 100 ms of 8-byte frames
            await asyncio.sleep(0.05)
            self.assertAlmostEqual(sink.pending_us(), 100_000, delta=2)
            os.read(read_fd, 48000 * 8 // 20)                                # 50 ms consumed
            self.assertAlmostEqual(sink.pending_us(), 50_000, delta=2)
        finally:
            writer.close()
            os.close(read_fd)

    async def test_without_a_process_the_fill_is_simply_unknown(self):
        self.assertIsNone(PipeWireSink().pending_us())


if __name__ == "__main__":
    unittest.main()
