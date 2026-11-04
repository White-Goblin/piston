import unittest

from piston_device.proto.code import PairingCode


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class PairingCodeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.pc = PairingCode(clock=self.clock, ttl=300, max_attempts=5)

    def test_shape(self):
        self.assertEqual(len(self.pc.code), 6)
        self.assertTrue(self.pc.code.isdigit())
        self.assertTrue(self.pc.valid)

    def test_expires(self):
        self.clock.t = 299
        self.assertTrue(self.pc.valid)
        self.clock.t = 300
        self.assertTrue(self.pc.expired)
        self.assertFalse(self.pc.valid)

    def test_single_use(self):
        self.pc.consume()
        self.assertFalse(self.pc.valid)

    def test_invalid_after_five_wrong_attempts(self):
        for i in range(4):
            self.pc.fail()
            self.assertTrue(self.pc.valid, i)
        self.pc.fail()
        self.assertFalse(self.pc.valid)
        self.assertEqual(self.pc.attempts_left, 0)

    def test_new_code_resets_everything(self):
        for _ in range(5):
            self.pc.fail()
        self.pc.consume()
        self.clock.t = 1000
        old = self.pc.code
        self.pc.new_code()
        self.assertTrue(self.pc.valid)
        self.assertEqual(self.pc.attempts, 0)
        self.assertEqual(len(self.pc.code), 6)
        self.assertIsInstance(old, str)

    def test_codes_vary(self):
        self.assertGreater(len({PairingCode(clock=self.clock).code for _ in range(20)}), 1)
