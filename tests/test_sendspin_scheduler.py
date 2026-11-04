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


if __name__ == "__main__":
    unittest.main()
