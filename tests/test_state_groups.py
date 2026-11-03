import asyncio
import os
import unittest
from unittest import mock

from . import use_temp_settings_dir
from decky_ha import state as state_module
from decky_ha.controllers import ControllersMixin
from decky_ha.proto import PROTOCOL_VERSION, sensors as sensor_table
from decky_ha.sensors import SensorsMixin
from decky_ha.settings import SettingsMixin
from decky_ha.state import StateMixin, detect_session


class FakeServer:
    def __init__(self):
        self.states, self.events = [], []

    def push_state(self, name, data):
        self.states.append((name, data))

    def push_event(self, name, data=None):
        self.events.append((name, data))


class Host(SettingsMixin, StateMixin, SensorsMixin, ControllersMixin):
    def __init__(self):
        self._init_state()
        self.controller_state, self.os_controllers, self.readings = {}, {}, None
        self.sensor_wake = asyncio.Event()
        self.sensor_values, self.cpu_prev = {}, None


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        use_temp_settings_dir()
        self.h = Host()
        self.h.loop = asyncio.get_running_loop()
        self.server = FakeServer()
        self.h.server = self.server

    def test_push_caches_and_sends_and_snapshot_has_everything(self):
        self.h._push("sensors", {"cpu_load": 3})
        self.assertEqual(self.server.states, [("sensors", {"cpu_load": 3})])
        self.assertEqual(self.h._snapshot(), {"sensors": {"cpu_load": 3}})

    def test_push_without_a_connection_still_caches(self):
        self.h.server = None
        self.h._push("a", {"x": 1})
        self.assertEqual(self.h._snapshot(), {"a": {"x": 1}})

    async def test_device_group(self):
        info = {"interface": "enp1", "kind": "wired", "mac": "aa:bb:cc:dd:ee:ff", "broadcast": "10.0.0.255",
                "supported": True, "wol_enabled": False, "warnings": [], "error": ""}
        with mock.patch.object(state_module, "wol_info", lambda: info):
            await self.h._push_device()
            await self.h._push_device()  # unchanged: not pushed twice
        group = self.h.groups["device"]
        self.assertEqual((group["mac"], group["protocol"], group["wol"]["wol_enabled"]),
                         ("aa:bb:cc:dd:ee:ff", PROTOCOL_VERSION, False))
        self.assertEqual(len([s for s in self.server.states if s[0] == "device"]), 1)

    async def test_device_group_survives_a_failing_probe(self):
        def boom():
            raise OSError("no network")
        with mock.patch.object(state_module, "wol_info", boom):
            await self.h._push_device()
        self.assertEqual(self.h.groups["device"]["mac"], "")

    async def test_sensor_loop_pushes_and_obeys_the_interval(self):
        self.h._read_sensors = lambda: {"cpu_load": 12.5, "ip": "10.0.0.2"}
        self.h._update_settings({"sensor_interval": 30})
        waits = []

        async def fake_wait(coro, timeout):
            waits.append(timeout)
            coro.close()
            raise asyncio.CancelledError

        with mock.patch("decky_ha.sensors.asyncio.wait_for", fake_wait):
            with self.assertRaises(asyncio.CancelledError):
                await self.h._sensor_loop()
        self.assertEqual(waits, [30])
        self.assertEqual(self.h.groups["sensors"], {"cpu_load": 12.5, "ip": "10.0.0.2"})

    async def test_sensor_read_failure_does_not_stop_the_loop(self):
        def boom():
            raise OSError("proc gone")
        self.h._read_sensors = boom

        async def fake_wait(coro, timeout):
            coro.close()
            raise asyncio.CancelledError

        with mock.patch("decky_ha.sensors.asyncio.wait_for", fake_wait):
            with self.assertRaises(asyncio.CancelledError):
                await self.h._sensor_loop()
        self.assertNotIn("sensors", self.h.groups)

    def test_sensor_table_describes_every_reported_key(self):
        for key in ("cpu_temp", "gpu_load", "uptime", "ip", "wifi_signal", "battery_bq27441"):
            self.assertIsNotNone(sensor_table.describe(key), key)
        self.assertIsNone(sensor_table.describe("nonsense"))
        self.assertEqual(sensor_table.describe("battery_main_pack")[0], "Battery main pack")

    async def test_controllers_group_has_connected_and_seen(self):
        await self.h.report_controllers([
            {"serial": "ABC-123", "name": "Steam Controller", "battery": 76, "charging": False, "wireless": True}])
        group = self.h.groups["controllers"]
        self.assertEqual(group["connected"]["abc123"]["battery"], 76)
        self.assertFalse(group["connected"]["abc123"]["stale"])
        self.assertEqual(group["seen"], [{"serial": "abc123", "name": "Steam Controller"}])
        # the pad disconnects: still seen, no longer connected
        await self.h.report_controllers([])
        group = self.h.groups["controllers"]
        self.assertEqual(group["connected"], {})
        self.assertEqual(len(group["seen"]), 1)

    async def test_bluetooth_pad_without_reading_reports_last_stale_value(self):
        self.h.os_controllers = {"aabbcc": {"name": "Pro 2", "battery": 52, "charging": False, "wireless": True}}
        self.h._remember_controllers()
        self.h.os_controllers = {"aabbcc": {"name": "Pro 2", "battery": None, "charging": None, "wireless": True}}
        self.h._publish_controllers()
        pad = self.h.groups["controllers"]["connected"]["aabbcc"]
        self.assertEqual((pad["battery"], pad["stale"], pad["charging"]), (52, True, None))

    def test_hid_battery_entries_are_read(self):
        base = os.path.join(os.environ.get("TMPDIR", "/tmp"), "psdir")
        with mock.patch("decky_ha.controllers.POWER_SUPPLY", base), mock.patch.object(
                Host, "_read_file", staticmethod(lambda path: {"capacity": "52", "status": "Discharging",
                                                                           "model_name": "PRO2 Black"}.get(path.rsplit("/", 1)[1], ""))), \
                mock.patch("decky_ha.controllers.os.listdir", lambda p: ["hid-aa:bb:cc:dd:ee:ff-battery", "BAT0"]):
            found = self.h._read_hid_batteries()
        self.assertEqual(found["aabbccddeeff"], {"name": "PRO2 Black", "battery": 52, "charging": False, "wireless": True})


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def test_detection_from_process_names(self):
        self.assertEqual(detect_session({"steam", "gamescope-wl", "bash"}), "game")
        self.assertEqual(detect_session({"plasmashell", "kwin_wayland", "steam"}), "desktop")
        self.assertEqual(detect_session({"bash"}), "unknown")
        self.assertEqual(detect_session({"gamescope", "plasmashell"}), "game")

    async def test_session_change_pushes_the_device_group_and_clears_the_app(self):
        use_temp_settings_dir()
        h = Host()
        h.loop = asyncio.get_running_loop()
        h.server = FakeServer()
        h.app_state = {"running": True, "appid": "42", "name": "X", "screen": "game"}
        h.app_focus, h.app_reported = "", True
        h._set_app_state = lambda **c: h._push("apps", {"screen": "desktop" if c else "game"})
        with mock.patch.object(state_module, "detect_session", lambda: "desktop"):
            await h._check_session()
            await h._check_session()  # unchanged: nothing more is pushed
        self.assertEqual(h.session, "desktop")
        self.assertEqual(h.groups["device"]["session"], "desktop")
        self.assertEqual(h.groups["apps"], {"screen": "desktop"})
        self.assertEqual(len([s for s in h.server.states if s[0] == "device"]), 1)

    async def test_unknown_detection_keeps_the_last_session(self):
        use_temp_settings_dir()
        h = Host()
        h.loop = asyncio.get_running_loop()
        h.session = "game"
        with mock.patch.object(state_module, "detect_session", lambda: "unknown"):
            await h._check_session()
        self.assertEqual(h.session, "game")


class AnnounceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sleep_and_shutdown_are_sent_and_flushed(self):
        use_temp_settings_dir()
        h = Host()
        h.server = FakeServer()
        h.server.flush = mock.AsyncMock()
        await h._announce_sleep()
        await h._announce_shutdown("shutdown")
        self.assertEqual(h.server.events, [("going_to_sleep", {"reason": "suspend"}), ("shutting_down", {"reason": "shutdown"})])
        self.assertEqual(h.server.flush.await_count, 2)

    async def test_nothing_to_flush_without_a_server(self):
        use_temp_settings_dir()
        h = Host()
        await h._announce_sleep()                      # must not raise

    async def test_protection_follows_the_lifecycle_module(self):
        use_temp_settings_dir()
        h = Host()
        self.assertFalse(h._sleep_protected())
        self.assertFalse(h._lifecycle_active())
        h.lifecycle = mock.Mock(active=True, protecting=False)
        self.assertTrue(h._lifecycle_active())
        self.assertFalse(h._sleep_protected())         # monitor up but no inhibitor: our own announcement stays
        h.lifecycle.protecting = True
        self.assertTrue(h._sleep_protected())


class HoldTests(unittest.TestCase):
    def setUp(self):
        use_temp_settings_dir()
        self.h = Host()
        self.h.server = FakeServer()

    def test_held_pushes_are_sent_on_release_only_when_changed(self):
        self.h.groups = {"audio": {"volume": 10}, "sensors": {"cpu": 1}}
        before = dict(self.h.groups)
        token = self.h._hold_pushes()
        self.h._push("audio", {"volume": 10})            # same as before
        self.h._push("sensors", {"cpu": 2})              # changed
        self.h._push("sensors", {"cpu": 3})              # latest value wins
        self.assertEqual(self.h.server.states, [])
        self.h._release_pushes(token, before)
        self.assertEqual(self.h.server.states, [("sensors", {"cpu": 3})])
        self.h._push("audio", {"volume": 11})            # no longer held
        self.assertEqual(self.h.server.states[-1], ("audio", {"volume": 11}))

    def test_releasing_an_old_hold_does_not_end_a_newer_one(self):
        old = self.h._hold_pushes()
        new = self.h._hold_pushes()
        self.h._release_pushes(old, {})
        self.h._push("x", {"v": 1})
        self.assertEqual(self.h.server.states, [])       # the newer hold is still collecting
        self.h._release_pushes(new, {})
        self.assertEqual(self.h.server.states, [("x", {"v": 1})])


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_wall_clock_jump_is_a_resume(self):
        use_temp_settings_dir()
        h = Host()
        announced = []

        async def announce():
            announced.append(1)

        h._announce_resume = announce
        walls = iter([1000.0, 1002.0, 1500.0, 1502.0, 1504.0])  # the third reading jumps ~500 s
        monos = iter([50.0, 52.0, 54.0, 56.0, 58.0])
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) > 3:
                raise asyncio.CancelledError

        with mock.patch.object(state_module.time, "time", lambda: next(walls)), \
                mock.patch.object(state_module.time, "monotonic", lambda: next(monos)), \
                mock.patch.object(state_module.asyncio, "sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await h._resume_loop()
        self.assertEqual(announced, [1])

    async def test_the_wall_clock_fallback_stays_quiet_while_logind_announces_resumes(self):
        use_temp_settings_dir()
        h = Host()
        h.loop = asyncio.get_running_loop()
        h.server = FakeServer()
        h.lifecycle = mock.Mock(active=True, protecting=True)
        walls = iter([1000.0, 1500.0, 1502.0, 1504.0])          # the second reading jumps ~500 s
        monos = iter([50.0, 52.0, 54.0, 56.0])
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) > 2:
                raise asyncio.CancelledError

        with mock.patch.object(state_module.time, "time", lambda: next(walls)), \
                mock.patch.object(state_module.time, "monotonic", lambda: next(monos)), \
                mock.patch.object(state_module.asyncio, "sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await h._resume_loop()
        self.assertEqual(h.server.events, [])


class EnableCommandTests(unittest.TestCase):
    def test_wired_command(self):
        from decky_ha.wol import enable_command
        info = {"interface": "enp5s0", "kind": "wired", "supported": True, "wol_enabled": False, "connection": "Wired connection 1"}
        self.assertEqual(enable_command(info), "sudo nmcli connection modify 'Wired connection 1' 802-3-ethernet.wake-on-lan magic"
                                               " && sudo nmcli device reapply enp5s0")

    def test_wireless_command_and_awkward_names_are_quoted(self):
        from decky_ha.wol import enable_command
        info = {"interface": "wlan0", "kind": "wireless", "supported": True, "wol_enabled": False, "connection": "Bob's \"Wi-Fi\""}
        cmd = enable_command(info)
        self.assertIn("802-11-wireless.wake-on-wlan magic", cmd)
        self.assertNotIn("Bob's \"Wi-Fi\" ", cmd)  # never pasted raw into a shell line
        import shlex
        self.assertEqual(shlex.split(cmd.split(" && ")[0])[4], "Bob's \"Wi-Fi\"")

    def test_no_command_when_nothing_to_do_or_nothing_possible(self):
        from decky_ha.wol import enable_command
        base = {"interface": "enp5s0", "kind": "wired", "supported": True, "wol_enabled": False, "connection": "c"}
        for change in ({"wol_enabled": True}, {"supported": False}, {"connection": ""}, {"interface": ""}):
            self.assertEqual(enable_command({**base, **change}), "", change)

    def test_the_panel_callable_adds_the_command(self):
        from unittest import mock
        from decky_ha import wol as wol_module
        use_temp_settings_dir()
        h = type("H", (wol_module.WolMixin,), {})()
        info = {"interface": "enp5s0", "kind": "wired", "supported": True, "wol_enabled": False, "connection": "c"}
        with mock.patch.object(wol_module, "wol_info", lambda: dict(info)):
            result = asyncio.run(h.get_wol_info())
        self.assertIn("sudo nmcli connection modify c 802-3-ethernet.wake-on-lan magic", result["command"])
        self.assertFalse(hasattr(h, "enable_wol"))
