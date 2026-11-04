import math
import random
import unittest

from piston_device.sendspin import clock
from piston_device.sendspin.clock import ClockFilter


def feed(f, n, offset=250_000, max_error=1000, interval=1_000_000, start=1_000_000, drift=0.0, noise=0.0, rng=None):
    t = start
    for _ in range(n):
        true_offset = offset + drift * (t - start)
        m = round(true_offset + (rng.gauss(0, noise) if rng else 0))
        f.update(m, max_error, t)
        t += interval
    return t


class SampleTests(unittest.TestCase):
    def test_a_symmetric_exchange_gives_the_offset_and_half_the_round_trip(self):
        # server is 5000 us ahead; 300 us each way; the server takes 50 us to answer
        t1 = 1_000_000
        t2 = t1 + 300 + 5000
        t3 = t2 + 50
        t4 = t3 - 5000 + 300
        self.assertEqual(clock.sample_from_exchange(t1, t2, t3, t4), (5000, 300, t4))

    def test_integer_division_truncates_toward_zero(self):
        self.assertEqual(clock.sample_from_exchange(0, 3, 3, 0)[0], 3)       # (3 + 3) / 2
        self.assertEqual(clock.sample_from_exchange(0, 1, 1, 0)[0], 1)
        self.assertEqual(clock.sample_from_exchange(0, 0, 1, 0)[0], 0)       # 1 / 2 truncates to 0
        self.assertEqual(clock.sample_from_exchange(10, 9, 9, 10)[0], -1)    # -2 / 2
        self.assertEqual(clock.sample_from_exchange(10, 10, 9, 10)[0], 0)    # -1 / 2 truncates to 0, not -1
        self.assertEqual(clock._div2(-3), -1)
        self.assertEqual(clock._div2(3), 1)

    def test_the_best_of_a_burst_is_the_lowest_max_error(self):
        burst = [(100, 900, 10), (105, 300, 20), (90, 500, 30), (0, 0, 40), (1, -5, 50)]
        self.assertEqual(clock.best_of_burst(burst), (105, 300, 20))
        self.assertIsNone(clock.best_of_burst([(1, 0, 1)]))
        self.assertIsNone(clock.best_of_burst([]))


class FilterTests(unittest.TestCase):
    def test_a_new_filter_is_not_converged(self):
        f = ClockFilter()
        self.assertFalse(f.converged)
        self.assertEqual(f.error_us, -1)

    def test_the_first_two_samples_follow_the_spec(self):
        f = ClockFilter()
        self.assertTrue(f.update(12_345, 500, 1_000_000))
        self.assertEqual((f.count, f.offset, f.drift, f.converged), (1, 12345.0, 0.0, False))
        self.assertEqual(f.p_oo, (0.5 * 500) ** 2)
        f = ClockFilter()
        f.update(1000, 500, 1_000_000)
        f.update(2000, 500, 2_000_000)
        self.assertEqual((f.count, f.offset, f.converged), (2, 2000.0, True))
        self.assertAlmostEqual(f.drift, 0.001)

    def test_non_monotonic_and_non_positive_error_samples_are_discarded(self):
        f = ClockFilter()
        f.update(1000, 500, 1_000_000)
        before = (f.count, f.offset, f.last_update)
        for args in ((9, 500, 1_000_000), (9, 500, 999_999), (9, 0, 2_000_000), (9, -1, 2_000_000)):
            self.assertFalse(f.update(*args))
        self.assertEqual((f.count, f.offset, f.last_update), before)

    def test_reset(self):
        f = ClockFilter()
        feed(f, 5)
        f.reset()
        self.assertEqual((f.count, f.last_update, f.offset, f.drift, f.p_od, f.p_dd), (0, 0, 0.0, 0.0, 0.0, 0.0))
        self.assertTrue(math.isinf(f.p_oo))
        self.assertFalse(f.use_drift)

    def test_a_constant_offset_is_tracked_to_a_microsecond(self):
        f = ClockFilter()
        end = feed(f, 20)
        for t in (end, end + 5_000_000):
            self.assertLessEqual(abs(f.compute_server_time(t) - (t + 250_000)), 1)

    def test_conversions_are_inverses_with_and_without_drift(self):
        for drift in (0.0, 1e-4):
            f = ClockFilter()
            end = feed(f, 50, drift=drift)
            for t in (end, end + 10_000_000):
                self.assertLessEqual(abs(f.compute_client_time(f.compute_server_time(t)) - t), 1, msg=f"drift {drift}")

    def test_drift_is_only_applied_when_significant(self):
        f = ClockFilter()
        feed(f, 60, drift=5e-5)
        self.assertTrue(f.use_drift)
        flat = ClockFilter()
        feed(flat, 60)
        self.assertFalse(flat.use_drift)

    def test_a_noisy_clock_with_drift_converges_to_well_under_a_millisecond(self):
        rng = random.Random(1729)
        f = ClockFilter()
        end = feed(f, 200, offset=250_000, drift=5e-8, noise=50, max_error=500, rng=rng, interval=1_000_000)
        truth = 250_000 + 5e-8 * (end - 1_000_000)
        self.assertLess(abs(f.compute_server_time(end) - end - truth), 200)
        self.assertLess(f.error_us, 500)

    def test_an_offset_jump_is_recovered_by_adaptive_forgetting(self):
        f = ClockFilter()
        t = feed(f, 150, max_error=1000)  # past min_samples, so forgetting is active
        errors = []
        for i in range(80):
            f.update(300_000, 1000, t + i * 1_000_000)
            errors.append(abs(f.offset - 300_000))
        self.assertLess(errors[0], 50_000)       # the first sample already moves most of the way
        self.assertLess(errors[6], 500)          # and a handful of samples settle it (measured: about 100 us)
        self.assertLess(errors[-1], 500)

    def test_conversion_before_any_sample_is_the_identity_shift_of_zero(self):
        f = ClockFilter()
        self.assertEqual(f.compute_server_time(123), 123)
        self.assertEqual(f.compute_client_time(123), 123)

    def test_rounding_goes_away_from_zero_on_halves(self):
        self.assertEqual([clock.round_half_away(x) for x in (0.5, 1.5, 2.5, -0.5, -1.5, -2.5, 0.49, -0.49)], [1, 2, 3, -1, -2, -3, 0, 0])
        f = ClockFilter()
        f.update(1, 1, 10)  # offset 1.0 exactly; shift by 0.5 through a crafted state
        f.offset = 2.5
        self.assertEqual(f.compute_server_time(100), 103)
        f.offset = -2.5
        self.assertEqual(f.compute_server_time(100), 97)


if __name__ == "__main__":
    unittest.main()
