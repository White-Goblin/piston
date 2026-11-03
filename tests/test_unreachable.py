import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import IssueRegistry

from custom_components.decky_home_assistant.unreachable import CHECK_EVERY, UNREACHABLE_AFTER, UnreachableMonitor
from .entityutil import make_coordinator

T0 = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
ISSUE = "device_unreachable_dev123abc456"


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        IssueRegistry.created.clear()
        IssueRegistry.deleted.clear()
        self.now = [T0]
        self.coord = make_coordinator()
        self.coord._now = lambda: self.now[0]          # the coordinator stamps last_connected with the same fake clock
        self.refreshed = 0
        self.timers = []

        async def refresh():
            self.refreshed += 1

        def track(hass, action, interval):
            self.timers.append((action, interval))
            return lambda: self.timers.clear()

        self.monitor = UnreachableMonitor(self.coord.hass, self.coord.entry, self.coord, refresh, now=lambda: self.now[0], track_interval=track)

    def created(self):
        return [i for i, _ in IssueRegistry.created]

    def advance(self, **delta):
        self.now[0] += timedelta(**delta)

    async def tick(self):
        action, _ = self.timers[0]
        await action(None)

    async def test_the_check_runs_hourly(self):
        self.monitor.start()
        self.assertEqual([interval for _, interval in self.timers], [CHECK_EVERY])
        self.assertEqual(CHECK_EVERY, timedelta(hours=1))

    async def test_not_created_before_24_hours_and_created_after(self):
        self.monitor.start()                                         # never seen: the clock starts now
        self.advance(hours=23, minutes=59)
        await self.tick()
        self.assertEqual(self.created(), [])
        self.advance(minutes=2)                                      # 24 h and 1 min without a connection
        await self.tick()
        self.assertEqual(self.created(), [ISSUE])
        _, kw = IssueRegistry.created[0]
        self.assertEqual((kw["is_fixable"], kw["severity"], kw["translation_key"], kw["translation_placeholders"]),
                         (False, "warning", "device_unreachable", {"name": "Box"}))

    async def test_exactly_24_hours_is_not_yet_unreachable(self):
        self.monitor.start()
        self.now[0] = T0 + UNREACHABLE_AFTER
        await self.tick()
        self.assertEqual(self.created(), [])

    async def test_it_is_created_once_not_every_hour(self):
        self.monitor.start()
        for _ in range(5):
            self.advance(hours=25)
            await self.tick()
        self.assertEqual(self.created(), [ISSUE])

    async def test_a_connection_removes_it_at_once(self):
        self.monitor.start()
        self.advance(hours=30)
        await self.tick()
        self.coord.on_connection(True)                               # the coordinator notifies its listeners
        self.assertEqual(IssueRegistry.deleted, [ISSUE])

    async def test_a_connection_is_also_noticed_by_the_hourly_check(self):
        self.monitor.start()
        self.advance(hours=30)
        await self.tick()
        self.coord.connected = True                                  # no notification reached us
        await self.tick()
        self.assertEqual(IssueRegistry.deleted, [ISSUE])

    async def test_after_a_restart_the_stored_time_counts(self):
        self.coord.restore({}, T0 - timedelta(hours=30))             # offline for 30 h before Home Assistant restarted
        self.monitor.start()
        self.assertEqual(self.created(), [ISSUE])                    # created by the check at startup

    async def test_a_recently_seen_device_after_a_restart_is_fine(self):
        self.coord.restore({}, T0 - timedelta(hours=3))
        self.monitor.start()
        self.assertEqual(self.created(), [])

    async def test_a_device_never_seen_is_measured_from_the_setup_not_from_the_beginning_of_time(self):
        self.monitor.start()
        self.assertIsNone(self.coord.last_connected)
        self.assertEqual(self.created(), [])

    async def test_a_lost_connection_restarts_the_clock(self):
        self.monitor.start()
        self.advance(hours=200)
        self.coord.on_connection(True)
        self.coord.on_connection(False)                              # lost now, after a long connection
        await self.tick()
        self.assertEqual(self.created(), [])
        self.advance(hours=25)
        await self.tick()
        self.assertEqual(self.created(), [ISSUE])

    async def test_while_connected_each_check_refreshes_the_stored_time(self):
        self.monitor.start()
        await self.tick()
        self.assertEqual(self.refreshed, 0)                          # not connected: nothing to refresh
        self.coord.connected = True
        await self.tick()
        await self.tick()
        self.assertEqual(self.refreshed, 2)

    async def test_stopping_removes_the_listener_and_the_timer(self):
        stop = self.monitor.start()
        stop()
        self.assertEqual(self.timers, [])
        self.advance(hours=30)
        self.coord.on_connection(True)
        self.assertEqual(IssueRegistry.deleted, [])                  # no longer listening


if __name__ == "__main__":
    unittest.main()
