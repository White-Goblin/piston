import unittest

from piston_device.pointer import PointerTracker


class TrackerTests(unittest.TestCase):
    def test_a_slow_move_has_a_gain_close_to_the_scale(self):
        t = PointerTracker(1000, 800, 2.0)
        t.park()
        ix, iy = t.move(1, 0)
        self.assertEqual((ix, iy), (2, 0))                      # gain (1 + 0.05) * 2 = 2.1 -> 2 now, 0.1 carried
        self.assertAlmostEqual(t.x, 2.1)

    def test_a_fast_move_gains_up_to_the_cap(self):
        t = PointerTracker(10000, 10000, 1.0)
        t.park()
        ix, _ = t.move(100, 0)                                  # capped at 30 units of gain: 1 + 1.5 = 2.5
        self.assertEqual(ix, 250)

    def test_the_fraction_is_carried_so_nothing_is_lost(self):
        t = PointerTracker(1000, 800, 1.0)
        t.park()
        total = sum(t.move(0.3, 0)[0] for _ in range(1000))
        self.assertAlmostEqual(total, 0.3 * 1.015 * 1000, delta=1)
        self.assertAlmostEqual(t.x, 0.3 * 1.015 * 1000, delta=0.001)

    def test_the_position_is_clamped_to_the_screen_but_the_carry_is_not_lost_outside(self):
        t = PointerTracker(100, 100, 1.0)
        t.park()
        t.move(-50, -50)
        self.assertEqual((t.x, t.y), (0, 0))
        for _ in range(50):
            t.move(40, 40)
        self.assertEqual((t.x, t.y), (99, 99))

    def test_negative_moves_truncate_toward_zero_and_carry(self):
        t = PointerTracker(1000, 800, 1.0)
        self.assertEqual(t.move(-0.5, 0), (0, 0))
        self.assertEqual(t.move(-0.5, 0)[0], -1)

    def test_set_screen_converts_css_px_to_device_px_and_clamps(self):
        t = PointerTracker()
        t.x, t.y = 5000, 5000
        self.assertTrue(t.set_screen(1377, 775, 2.79))
        self.assertEqual((t.width, t.height), (3841, 2162))
        self.assertEqual((t.x, t.y), (3840, 2161))
        self.assertEqual(t.scale, 2.79)

    def test_nonsense_screen_values_are_ignored(self):
        t = PointerTracker(1280, 800, 1.0)
        for args in ((50, 800, 1), (800, 50, 1), (800, 800, 0.1)):
            self.assertFalse(t.set_screen(*args))
        self.assertEqual((t.width, t.height, t.scale), (1280, 800, 1.0))

    def test_css_position_divides_by_the_scale(self):
        t = PointerTracker(3840, 2160, 2.0)
        t.snap_to(1000, 501)
        self.assertEqual(t.css_position(), (500, 250))

    def test_take_sent_reports_what_was_injected_and_resets(self):
        t = PointerTracker(1000, 800, 1.0)
        t.park()
        t.move(10, 0)
        t.move(0, 10)
        sent = t.take_sent()
        self.assertEqual(sent, (15, 15))   # 10 px of travel has gain 1 + 0.05 * 10 = 1.5
        self.assertEqual(t.take_sent(), (0, 0))

    def test_snap_clamps(self):
        t = PointerTracker(100, 100, 1.0)
        t.snap_to(-5, 500)
        self.assertEqual((t.x, t.y), (0.0, 99.0))

    def test_speed_rises_with_movement_and_clicking_resets_it(self):
        t = PointerTracker(1000, 800, 1.0)
        t.move(20, 0)
        self.assertGreater(t.speed, 5)
        t.clicked()
        self.assertEqual(t.speed, 0.0)

    def test_no_io_or_decky_dependency(self):
        import piston_device.pointer as mod
        self.assertNotIn("decky", vars(mod))
