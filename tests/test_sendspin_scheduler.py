import unittest

from piston_device.sendspin.scheduler import Drop, Flush, Scheduler, Silence, Write

RATE, FB = 48000, 4
CHUNK_FRAMES = 960  # 20 ms
CHUNK = bytes(CHUNK_FRAMES * FB)


class Fixture:
    """Server time = local time + offset; the local clock is set by the test."""

    def __init__(self, offset=1_000_000, **kw):
        self.t = 10_000_000
        self.offset = offset
        self.s = Scheduler(lambda ts: ts - self.offset, lambda: self.t, rate=RATE, frame_bytes=FB, **kw)

    def server(self, local_us):
        return int(local_us + self.offset)


class SchedulerTests(unittest.TestCase):
    def test_a_chunk_is_not_released_before_it_is_close_to_playing(self):
        f = Fixture()
        f.s.submit(f.server(f.t + 2_000_000), CHUNK)  # plays in 2 s
        self.assertEqual(f.s.poll(), [])
        self.assertAlmostEqual(f.s.next_due_in_us(), 2_000_000 - 50_000 - 100_000)
        f.t += 1_850_000
        self.assertEqual(f.s.next_due_in_us(), 0)
        actions = f.s.poll()
        self.assertEqual([type(a) for a in actions], [Silence, Write])

    def test_the_first_chunk_is_preceded_by_exactly_the_gap_to_its_play_time(self):
        f = Fixture()
        target = f.t + 120_000          # earliest possible is now + 50 ms latency
        f.s.submit(f.server(target), CHUNK)
        silence, write = f.s.poll()
        self.assertEqual(silence.frames, round((target - (f.t + 50_000)) * RATE / 1e6))
        self.assertEqual((write.at_us, len(write.pcm)), (target, len(CHUNK)))

    def test_contiguous_chunks_are_appended_back_to_back_without_silence(self):
        f = Fixture()
        base = f.t + 120_000
        for i in range(10):
            f.s.submit(f.server(base + i * 20_000), CHUNK)
        f.t = base - 60_000
        actions = []
        while f.s.queued_bytes:                      # only chunks inside the release window come out per poll
            actions += f.s.poll()
            f.t += 10_000
        writes = [a for a in actions if isinstance(a, Write)]
        silences = [a for a in actions if isinstance(a, Silence)]
        self.assertEqual(len(writes), 10)
        self.assertEqual(len(silences), 1)  # only the lead-in
        for a, b in zip(writes, writes[1:]):
            self.assertAlmostEqual(b.at_us - a.at_us, 20_000, places=3)

    def test_jitter_below_the_tolerance_does_not_change_the_audio(self):
        f = Fixture()
        base = f.t + 120_000
        f.s.submit(f.server(base), CHUNK)
        f.s.submit(f.server(base + 20_000 + 3_000), CHUNK)  # 3 ms off
        f.t = base - 60_000
        kinds = [type(a) for a in f.s.poll()]
        self.assertEqual(kinds, [Silence, Write, Write])

    def test_an_early_chunk_after_a_gap_is_preceded_by_silence_of_the_exact_length(self):
        f = Fixture()
        base = f.t + 120_000
        f.s.submit(f.server(base), CHUNK)
        f.s.submit(f.server(base + 20_000 + 40_000), CHUNK)  # a 40 ms hole before the second
        f.t = base - 60_000
        actions = f.s.poll()
        self.assertEqual([type(a) for a in actions], [Silence, Write, Silence, Write])
        self.assertEqual(actions[2].frames, 40_000 * RATE // 1_000_000)

    def test_a_chunk_that_is_wholly_late_is_dropped_and_the_next_one_gets_silence(self):
        f2 = Fixture()
        f2.s.submit(f2.server(f2.t - 500_000), CHUNK)  # arrives 500 ms after it should have played
        actions = f2.s.poll()
        self.assertEqual(actions, [Drop(CHUNK_FRAMES)])
        f2.s.submit(f2.server(f2.t + 200_000), CHUNK)
        f2.t += 100_000                                # now it is within the release window
        after = f2.s.poll()
        self.assertEqual([type(a) for a in after], [Silence, Write])

    def test_a_partly_late_chunk_is_trimmed_to_stay_in_sync(self):
        f = Fixture()
        # should have started 10 ms before the earliest possible play time (now + 50 ms latency)
        f.s.submit(f.server(f.t + 40_000), CHUNK)
        actions = f.s.poll()
        self.assertEqual(actions[0], Drop(round(10_000 * RATE / 1e6)))
        self.assertEqual(len(actions[1].pcm), (CHUNK_FRAMES - 480) * FB)

    def test_a_slow_consumer_that_starves_the_stream_resynchronizes_by_dropping(self):
        f = Fixture()
        base = f.t + 120_000
        for i in range(5):
            f.s.submit(f.server(base + i * 20_000), CHUNK)
        f.t = base - 60_000
        f.s.poll()
        f.t += 500_000                                  # nothing polled for half a second
        f.s.submit(f.server(base + 5 * 20_000), CHUNK)  # long past
        self.assertEqual(f.s.poll(), [Drop(CHUNK_FRAMES)])

    def test_clear_without_a_timestamp_drops_the_queue_and_asks_for_a_flush(self):
        f = Fixture()
        f.s.submit(f.server(f.t + 5_000_000), CHUNK)
        self.assertEqual(f.s.clear(), [Flush()])
        self.assertEqual((f.s.poll(), f.s.queued_bytes, f.s.cursor_us, f.s.next_due_in_us()), ([], 0, None, None))

    def test_clear_from_a_timestamp_keeps_earlier_chunks(self):
        f = Fixture()
        for i in range(4):
            f.s.submit(f.server(f.t + 3_000_000 + i * 20_000), CHUNK)
        self.assertEqual(f.s.clear(from_ts_us=f.server(f.t + 3_000_000 + 2 * 20_000)), [])
        self.assertEqual(f.s.queued_bytes, 2 * len(CHUNK))

    def test_keepalive_fills_with_silence_up_to_the_release_window_and_advances_the_cursor(self):
        f = Fixture()
        actions = f.s.keepalive()                                     # nothing queued: fill to now + latency + lookahead
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].frames, round(100_000 * RATE / 1e6))
        self.assertAlmostEqual(f.s.cursor_us, f.t + 50_000 + 100_000, delta=1)
        self.assertEqual(f.s.keepalive(), [])                         # already full
        f.t += 40_000
        again = f.s.keepalive()
        self.assertEqual(again[0].frames, round(40_000 * RATE / 1e6))

    def test_keepalive_never_goes_past_the_next_chunks_play_time(self):
        f = Fixture()
        target = f.t + 110_000                                        # inside the window: 60 ms after the earliest sample
        f.s.submit(f.server(target), CHUNK)
        (silence,) = f.s.keepalive()
        self.assertEqual(silence.frames, round(60_000 * RATE / 1e6))
        f.t += 5_000
        released = f.s.poll()                                         # the chunk is due: no gap, no trimming
        self.assertEqual([type(a) for a in released], [Write])
        self.assertAlmostEqual(released[0].at_us, target, delta=2)

    def test_the_first_chunk_after_keepalive_plays_in_sync(self):
        f = Fixture()
        target = f.t + 400_000
        f.s.submit(f.server(target), CHUNK)
        total = 0
        while f.s.next_due_in_us() > 0:
            f.t += 10_000
            total += sum(a.frames for a in f.s.keepalive())
        writes = [a for a in f.s.poll() if isinstance(a, Write)]
        self.assertEqual(len(writes), 1)
        self.assertAlmostEqual(writes[0].at_us, target, delta=5_000)   # within the tolerance
        self.assertGreater(total, 0)

    def test_keepalive_after_the_stream_ran_dry_restarts_from_the_earliest_possible_time(self):
        f = Fixture()
        f.s.keepalive()
        f.t += 2_000_000                                              # nothing polled for two seconds
        (silence,) = f.s.keepalive()
        self.assertEqual(silence.frames, round(100_000 * RATE / 1e6))

    def test_output_delay_moves_the_play_time_earlier(self):
        f = Fixture()
        f.s.output_delay_us = 30_000
        target = f.t + 200_000
        f.s.submit(f.server(target), CHUNK)
        f.t += 100_000
        actions = f.s.poll()
        self.assertEqual(actions[-1].at_us, target - 30_000)

    def test_bad_chunks_and_overflow_are_refused(self):
        f = Fixture(capacity_bytes=2 * len(CHUNK))
        self.assertFalse(f.s.submit(1, b""))
        self.assertFalse(f.s.submit(1, bytes(FB + 1)))
        self.assertTrue(f.s.submit(1, CHUNK))
        self.assertTrue(f.s.submit(2, CHUNK))
        self.assertFalse(f.s.submit(3, CHUNK))

    def test_a_new_stream_format_resets_everything(self):
        f = Fixture()
        f.s.submit(f.server(f.t + 100_000), CHUNK)
        f.s.set_format(44100, 4)
        self.assertEqual((f.s.queued_bytes, f.s.cursor_us, f.s.rate), (0, None, 44100))

    def test_playout_stays_aligned_over_a_long_run(self):
        f = Fixture()
        base = f.t + 150_000
        for i in range(500):
            f.s.submit(f.server(base + i * 20_000), CHUNK)
        writes = []
        while f.s.queued_bytes:
            f.t += 10_000
            writes += [a for a in f.s.poll() if isinstance(a, Write)]
        self.assertEqual(len(writes), 500)
        self.assertAlmostEqual(writes[-1].at_us, base + 499 * 20_000, delta=1)


class EightByteFrameTests(unittest.TestCase):
    """24-bit audio is carried as 32-bit samples: stereo frames of 8 bytes."""

    FB8 = 8

    def make(self, rate=44100):
        t = [10_000_000]
        s = Scheduler(lambda ts: ts - 1_000_000, lambda: t[0], rate=rate, frame_bytes=self.FB8)
        return s, t

    def test_chunks_are_checked_against_the_frame_size(self):
        s, _ = self.make()
        self.assertFalse(s.submit(1, bytes(4)))                      # not a whole 8-byte frame
        self.assertFalse(s.submit(1, bytes(12)))
        self.assertTrue(s.submit(1, bytes(16)))

    def test_a_partly_late_chunk_is_trimmed_on_a_frame_boundary(self):
        s, t = self.make(44100)
        chunk = bytes(range(256)) * 18 + bytes(range(256))[:0]       # 4608 bytes = 576 frames
        frames = len(chunk) // 8
        s.submit(t[0] + 1_000_000 + 40_000, chunk)                   # should have started 10 ms before the earliest play time
        actions = s.poll()
        drop, write = actions[0], actions[1]
        self.assertEqual(drop.frames, round(10_000 * 44100 / 1e6))
        self.assertEqual(len(write.pcm), (frames - drop.frames) * 8)
        self.assertEqual(write.pcm, chunk[drop.frames * 8:])         # the cut is exactly at a frame boundary

    def test_silence_is_counted_in_frames_whatever_their_size(self):
        s, t = self.make(48000)
        target = t[0] + 120_000
        s.submit(target + 1_000_000, bytes(8 * 960))
        silence, write = s.poll()
        self.assertEqual(silence.frames, round((target - (t[0] + 50_000)) * 48000 / 1e6))
        self.assertEqual(len(write.pcm), 8 * 960)

    def test_keepalive_and_the_cursor_do_not_depend_on_the_frame_size(self):
        a, ta = self.make(48000)
        b = Scheduler(lambda ts: ts - 1_000_000, lambda: ta[0], rate=48000, frame_bytes=4)
        self.assertEqual(a.keepalive(), b.keepalive())
        self.assertEqual(a.cursor_us, b.cursor_us)

    def test_a_decoder_style_format_change_resets_the_frame_size(self):
        s, _ = self.make()
        s.set_format(48000, 4)
        self.assertTrue(s.submit(1, bytes(4)))
        s.set_format(44100, 8)
        self.assertFalse(s.submit(1, bytes(4)))
        self.assertTrue(s.submit(1, bytes(8)))


class DecodeAtReleaseTests(unittest.TestCase):
    """Chunks are queued encoded and decoded when released."""

    def make(self, decode, capacity=100_000):
        t = [10_000_000]
        s = Scheduler(lambda ts: ts - 1_000_000, lambda: t[0], rate=48000, frame_bytes=4, capacity_bytes=capacity, decode=decode)
        return s, t

    def test_only_the_encoded_bytes_count_against_the_capacity(self):
        calls = []

        def expand(chunk):                                           # a 40-byte "encoded" chunk becomes 4000 bytes of PCM
            calls.append(len(chunk))
            return bytes(100 * len(chunk))
        s, t = self.make(expand, capacity=1000)
        for i in range(25):                                          # 25 x 40 = 1000 encoded bytes: exactly the capacity
            self.assertTrue(s.submit(t[0] + 1_000_000 + 5_000_000 + i * 20_000, bytes(40)))
        self.assertFalse(s.submit(t[0] + 9_000_000, bytes(40)))      # the 26th would overrun the server's own accounting
        self.assertEqual((s.queued_bytes, calls), (1000, []))        # nothing was decoded on arrival

    def test_the_decoder_runs_when_a_chunk_is_released_and_its_output_is_played(self):
        s, t = self.make(lambda chunk: chunk * 100)
        s.submit(t[0] + 1_000_000 + 400_000, bytes([1, 2, 3, 4]))     # plays 400 ms from now
        self.assertEqual(s.poll(), [])                               # not due yet: not decoded either
        t[0] += 300_000
        silence, write = s.poll()
        self.assertEqual(write.pcm, bytes([1, 2, 3, 4]) * 100)
        self.assertEqual((s.queued_bytes, s.decode_errors), (0, 0))

    def test_a_chunk_that_cannot_be_decoded_is_dropped_counted_and_replaced_by_silence(self):
        def decode(chunk):
            if chunk[0] == 99:
                raise ValueError("corrupt")
            return bytes(3840)
        s, t = self.make(decode)
        base = t[0] + 1_000_000 + 400_000
        s.submit(base, bytes([1]))
        s.submit(base + 20_000, bytes([99]))                         # this one is corrupt
        s.submit(base + 40_000, bytes([3]))
        t[0] += 300_000
        actions = s.poll()
        self.assertEqual(s.decode_errors, 1)
        kinds = [type(a) for a in actions]
        self.assertEqual(kinds, [Silence, Write, Silence, Write])    # lead-in, chunk 1, a 20 ms hole, chunk 3
        self.assertEqual(actions[2].frames, 960)
        self.assertAlmostEqual(actions[3].at_us - actions[1].at_us, 40_000, delta=5)

    def test_a_decoder_that_returns_nothing_or_partial_frames_counts_as_an_error(self):
        results = iter([b"", bytes(6), bytes(8)])
        s, t = self.make(lambda chunk: next(results))
        for i in range(3):
            s.submit(t[0] + 1_000_000 + 400_000 + i * 20_000, bytes(10))
        t[0] += 300_000
        s.poll()
        self.assertEqual(s.decode_errors, 2)                         # the empty one and the one that is not whole frames

    def test_without_a_decoder_nothing_changes(self):
        s, t = self.make(None)
        self.assertFalse(s.submit(1, bytes(3)))
        self.assertTrue(s.submit(t[0] + 1_000_000 + 400_000, bytes(3840)))
        t[0] += 300_000
        self.assertEqual(s.poll()[-1].pcm, bytes(3840))
        self.assertEqual(s.decode_errors, 0)

    def test_set_format_installs_the_new_streams_decoder_and_resets_the_count(self):
        s, t = self.make(lambda chunk: 1 / 0)
        s.submit(t[0] + 1_000_000, bytes(4))
        t[0] += 1_000_000
        s.poll()
        self.assertEqual(s.decode_errors, 1)
        s.set_format(44100, 8, decode=lambda chunk: bytes(8 * len(chunk)))
        self.assertEqual((s.decode_errors, s.queued_bytes, s.frame_bytes), (0, 0, 8))
        s.set_format(48000, 4)                                       # a PCM stream afterwards: no decoder
        self.assertIsNone(s.decode)

    def test_a_decode_that_is_slow_to_start_does_not_change_the_alignment_of_the_next_chunks(self):
        s, t = self.make(lambda chunk: bytes(3840))
        base = t[0] + 1_000_000 + 400_000
        for i in range(10):
            s.submit(base + i * 20_000, bytes(30))
        t[0] += 300_000
        writes = []
        while s.queued_bytes:
            writes += [a for a in s.poll() if isinstance(a, Write)]
            t[0] += 10_000
        self.assertEqual(len(writes), 10)
        self.assertAlmostEqual(writes[-1].at_us, base - 1_000_000 + 9 * 20_000, delta=5)


if __name__ == "__main__":
    unittest.main()

class DiscardDueTests(unittest.TestCase):
    def test_due_chunks_are_dropped_without_decoding_and_future_ones_stay(self):
        calls = []
        t = [10_000_000]
        s = Scheduler(lambda ts: ts - 1_000_000, lambda: t[0], rate=48000, frame_bytes=4, capacity_bytes=100_000,
                      decode=lambda b: calls.append(b) or b)
        s.submit(1_000_000 + 9_000_000, b"a" * 100)          # in the past
        s.submit(1_000_000 + 9_500_000, b"b" * 100)          # in the past
        s.submit(1_000_000 + 20_000_000, b"c" * 100)         # ten seconds ahead
        self.assertEqual(s.discard_due(), 2)
        self.assertEqual(s.queued_bytes, 100)
        self.assertEqual(calls, [])                           # nothing was decoded
        self.assertEqual(s.discard_due(), 0)
        t[0] = 19_900_000                                     # shortly before it is due
        actions = s.poll()
        self.assertTrue(any(type(a).__name__ == "Write" for a in actions))  # the future chunk still plays when its time comes
