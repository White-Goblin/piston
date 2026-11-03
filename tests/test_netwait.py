import unittest

from decky_ha.netwait import wait_for_network


class Clock:
    """A fake clock that only moves when the fake sleep is called."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class WaitForNetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()

    def wait(self, ready, **kw):
        return wait_for_network(ready=ready, sleep=self.clock.sleep, clock=self.clock.time, **kw)

    async def test_returns_at_once_when_the_network_is_already_up(self):
        self.assertTrue(await self.wait(lambda: "192.168.1.5"))
        self.assertEqual(self.clock.sleeps, [])

    async def test_returns_when_the_route_appears(self):
        answers = iter([None, None, None, "192.168.1.5"])
        self.assertTrue(await self.wait(lambda: next(answers), poll=0.25))
        self.assertEqual(self.clock.sleeps, [0.25, 0.25, 0.25])

    async def test_gives_up_at_the_cap(self):
        self.assertFalse(await self.wait(lambda: None, cap=1.0, poll=0.25))
        self.assertEqual(sum(self.clock.sleeps), 1.0)          # not earlier, not later

    async def test_errors_while_checking_count_as_not_ready(self):
        calls = []

        def ready():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("Network is unreachable")
            return "10.0.0.2"

        self.assertTrue(await self.wait(ready))
        self.assertEqual(len(calls), 3)

    async def test_a_zero_cap_checks_once(self):
        checks = []
        self.assertFalse(await self.wait(lambda: checks.append(1) or None, cap=0))
        self.assertEqual(len(checks), 1)


if __name__ == "__main__":
    unittest.main()
