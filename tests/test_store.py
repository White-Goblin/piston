import unittest
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.piston import store as store_module
from custom_components.piston.store import DeckyStore, stable_subset

KEY = "piston.e1"
T0 = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)

LIVE = {
    "device": {"device_id": "d1", "name": "Box", "protocol": 2, "session": "game", "mac": "aa:bb", "broadcast": "10.0.0.255",
               "wol": {"interface": "eth0", "supported": True}},
    "apps": {"apps": [{"appid": "1", "name": "A"}], "running": True, "appid": "1", "name": "A", "screen": "game", "playback": "playing"},
    "audio": {"mode": "system", "volume": 40, "muted": False},
    "controllers": {"connected": {"s1": {"battery": 50}}, "seen": [{"serial": "s1", "name": "Pad"}]},
    "sensors": {"cpu_load": 3, "uptime": 99},
    "pip": {"state": "hidden"},
}


class Fixture:
    """A coordinator stand-in: live groups, last_connected and connected, read by the store when it saves."""

    def __init__(self):
        self.groups, self.last_connected, self.connected = {}, None, False
        self.now = T0

    def current(self):
        return self.groups, self.last_connected, self.connected


class StableSubsetTests(unittest.TestCase):
    def test_keeps_structure_and_drops_volatile_values(self):
        self.assertEqual(stable_subset(LIVE), {
            "device": {"device_id": "d1", "name": "Box", "protocol": 2, "mac": "aa:bb", "broadcast": "10.0.0.255",
                       "wol": {"interface": "eth0", "supported": True}},
            "apps": {"apps": [{"appid": "1", "name": "A"}]},
            "audio": {"mode": "system"},
            "controllers": {"seen": [{"serial": "s1", "name": "Pad"}]},
            "sensors": {"keys": ["cpu_load", "uptime"]},
        })

    def test_a_group_with_no_storage_rule_is_not_stored(self):
        # Groups added later (sendspin) are live state until a spec says otherwise
        self.assertNotIn("sendspin", stable_subset({**LIVE, "sendspin": {"enabled": True, "status": "streaming"}}))

    def test_the_result_does_not_share_objects_with_the_live_state(self):
        subset = stable_subset(LIVE)
        subset["apps"]["apps"].append({"appid": "2"})
        self.assertEqual(len(LIVE["apps"]["apps"]), 1)

    def test_missing_or_odd_groups_are_skipped(self):
        self.assertEqual(stable_subset({}), {})
        self.assertEqual(stable_subset({"device": "oops", "sensors": {}, "audio": {"volume": 1}}), {})


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Store.reset()
        self.fx = Fixture()
        self.store = DeckyStore(HomeAssistant(), "e1", self.fx.current, now=lambda: self.fx.now)

    async def test_the_file_holds_only_stable_fields_and_the_last_connection(self):
        self.fx.groups, self.fx.connected = LIVE, True
        await self.store.async_save_now()
        (key, data), = Store.writes
        self.assertEqual(key, KEY)
        self.assertEqual(data["groups"], stable_subset(LIVE))
        self.assertEqual(data["last_connected"], T0.isoformat())     # connected: the last moment is now

    async def test_while_disconnected_the_last_connection_is_kept_not_renewed(self):
        self.fx.groups, self.fx.last_connected = LIVE, T0 - timedelta(hours=5)
        await self.store.async_save_now()
        self.assertEqual(Store.writes[-1][1]["last_connected"], (T0 - timedelta(hours=5)).isoformat())

    async def test_many_changes_that_are_not_stable_cause_no_writes_and_a_stable_change_one_delayed_save(self):
        self.fx.groups = {k: v for k, v in LIVE.items()}
        self.store.note_changed()
        self.assertEqual(Store.delays, [(KEY, 30)])
        for cpu in range(20):                                        # readings change every few seconds
            self.fx.groups = {**self.fx.groups, "sensors": {"cpu_load": cpu, "uptime": cpu}, "apps": {**LIVE["apps"], "running": bool(cpu % 2)}}
            self.store.note_changed()
        self.assertEqual(len(Store.delays), 1)                       # nothing stable changed: no new request
        self.assertEqual(Store.writes, [])                           # and nothing written before the delay ends
        await self.store._store.fire_delay()
        self.assertEqual(len(Store.writes), 1)

    async def test_a_new_stable_fact_requests_another_save(self):
        self.fx.groups = LIVE
        self.store.note_changed()
        self.fx.groups = {**LIVE, "apps": {**LIVE["apps"], "apps": [{"appid": "1", "name": "A"}, {"appid": "2", "name": "B"}]}}
        self.store.note_changed()
        self.assertEqual(len(Store.delays), 2)

    async def test_a_connection_change_requests_a_save(self):
        self.fx.groups = LIVE
        self.store.note_changed()
        self.fx.last_connected = T0
        self.store.note_changed()
        self.assertEqual(len(Store.delays), 2)

    async def test_round_trip(self):
        self.fx.groups, self.fx.connected = LIVE, True
        await self.store.async_save_now()
        again = DeckyStore(HomeAssistant(), "e1", Fixture().current)
        loaded = await again.async_load()
        self.assertEqual(loaded["groups"], stable_subset(LIVE))
        self.assertEqual(loaded["last_connected"], T0)

    async def test_a_restart_never_overwrites_known_structure_with_less(self):
        self.fx.groups, self.fx.connected = LIVE, True
        await self.store.async_save_now()
        fresh = Fixture()                                            # a new run: nothing reported yet
        restarted = DeckyStore(HomeAssistant(), "e1", fresh.current, now=lambda: T0)
        await restarted.async_load()
        restarted.note_changed()
        self.assertEqual(len(Store.delays), 0)                       # identical to what is stored: nothing to write
        fresh.groups = {"device": LIVE["device"]}                    # only the device group has arrived so far
        restarted.note_changed()
        await restarted.async_save_now()
        self.assertEqual(Store.writes[-1][1]["groups"], stable_subset(LIVE))   # apps, audio, ... kept from the file

    async def test_a_missing_file_loads_empty_without_a_warning(self):
        with self.assertNoLogs(store_module._LOGGER, "WARNING"):
            self.assertEqual(await self.store.async_load(), {"groups": {}, "last_connected": None, "update": {}})

    async def test_a_corrupt_file_loads_empty_and_logs_once(self):
        Store.files[KEY] = ValueError("not json")
        with self.assertLogs(store_module._LOGGER, "WARNING") as logs:
            self.assertEqual(await self.store.async_load(), {"groups": {}, "last_connected": None, "update": {}})
        self.assertEqual(len(logs.output), 1)

    async def test_a_newer_version_loads_empty_and_logs_once(self):
        Store.files[KEY] = {"version": 99, "data": {"groups": {"device": {"name": "x"}}}}
        with self.assertLogs(store_module._LOGGER, "WARNING") as logs:
            self.assertEqual(await self.store.async_load(), {"groups": {}, "last_connected": None, "update": {}})
        self.assertEqual(len(logs.output), 1)

    async def test_an_older_version_migrates_to_nothing_without_a_warning(self):
        Store.files[KEY] = {"version": 0, "data": {"anything": 1}}
        with self.assertNoLogs(store_module._LOGGER, "WARNING"):
            self.assertEqual(await self.store.async_load(), {"groups": {}, "last_connected": None, "update": {}})

    async def test_an_unexpected_shape_loads_empty_and_logs_once(self):
        for data in ({"groups": ["a"]}, {"groups": {"device": "x"}}, ["not", "a", "dict"]):
            Store.files[KEY] = {"version": 1, "data": data}
            with self.assertLogs(store_module._LOGGER, "WARNING") as logs:
                self.assertEqual((await DeckyStore(HomeAssistant(), "e1", self.fx.current).async_load())["groups"], {})
            self.assertEqual(len(logs.output), 1, data)

    async def test_a_bad_timestamp_is_ignored(self):
        Store.files[KEY] = {"version": 1, "data": {"groups": {}, "last_connected": "yesterday"}}
        self.assertIsNone((await self.store.async_load())["last_connected"])

    async def test_remove_deletes_the_file(self):
        self.fx.groups = LIVE
        await self.store.async_save_now()
        self.assertIn(KEY, Store.files)
        await DeckyStore.async_remove_file(HomeAssistant(), "e1")  # for an entry whose coordinator is gone
        self.assertNotIn(KEY, Store.files)
        self.assertEqual(Store.removed, [KEY])


if __name__ == "__main__":
    unittest.main()


class UpdateFactsTests(unittest.IsolatedAsyncioTestCase):
    """The plugin-update facts (version, auto setting, failure latch) are stored with the rest."""

    def setUp(self):
        Store.reset()

    def make(self, update, fx=None):
        fx = fx or Fixture()
        store = DeckyStore(HomeAssistant(), "e1", fx.current, now=lambda: fx.now, current_update=lambda: update)
        return store, update

    async def test_they_are_written_and_read_back(self):
        update = {"plugin_version": "0.4.0", "auto_update": False, "latch": {"version": "0.4.1", "reason": "hash"}}
        store, _ = self.make(update)
        await store.async_save_now()
        again = DeckyStore(HomeAssistant(), "e1", Fixture().current)
        loaded = await again.async_load()
        self.assertEqual(loaded["update"], update)

    async def test_wrong_shapes_are_dropped(self):
        store, _ = self.make({"plugin_version": 3, "auto_update": "yes", "latch": {"version": "1.0.0"}})
        await store.async_save_now()
        self.assertEqual((await DeckyStore(HomeAssistant(), "e1", Fixture().current).async_load())["update"], {})

    async def test_a_change_schedules_a_save_and_an_identical_state_does_not(self):
        update = {"auto_update": True}
        store, _ = self.make(update)
        await store.async_save_now()
        Store.delays.clear()
        store.note_changed()
        self.assertEqual(Store.delays, [])
        update["auto_update"] = False
        store.note_changed()
        self.assertEqual(len(Store.delays), 1)
