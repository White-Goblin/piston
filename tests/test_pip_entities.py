import unittest

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

from custom_components.decky_home_assistant import select as select_platform, sensor
from custom_components.decky_home_assistant.client import CommandError
from custom_components.decky_home_assistant.const import DEFAULT_OPTIONS
from custom_components.decky_home_assistant.select import OFF, PipCameraSelect, pip_options
from tests.ha_stubs import PLATFORM
from .entityutil import connect, make_coordinator
from .test_sensors import Collector


class FakeRelay:
    def __init__(self):
        self.shown, self.hid = [], 0
        self.error = None

    async def async_show(self, camera, options):
        if self.error:
            raise self.error
        self.shown.append((camera, options))
        return "session"

    async def async_hide(self):
        if self.error:
            raise self.error
        self.hid += 1


class State:
    def __init__(self, name):
        self.name = name


class States:
    def __init__(self, names):
        self.names = names

    def get(self, entity_id):
        return State(self.names[entity_id]) if entity_id in self.names else None


def select(options=None, names=None, **groups):
    coord = make_coordinator(options)
    coord.pip = FakeRelay()
    coord.hass.states = States(names or {})
    connect(coord, **groups)
    return PipCameraSelect(coord), coord


class OptionsTests(unittest.TestCase):
    def test_defaults_become_device_settings(self):
        self.assertEqual(pip_options(DEFAULT_OPTIONS), {"position": "top-right", "size": 0.4, "duration": 30000, "mute": True})

    def test_overrides_win_including_zero_duration_and_false_mute(self):
        got = pip_options(DEFAULT_OPTIONS, {"position": "left", "size": "extra_large", "duration": 0, "mute": False})
        self.assertEqual(got, {"position": "left", "size": 0.7, "duration": 0, "mute": False})

    def test_none_overrides_fall_back(self):
        self.assertEqual(pip_options({**DEFAULT_OPTIONS, "pip_size": "small", "pip_mute": False},
                                     {"position": None, "size": None, "duration": None, "mute": None}),
                         {"position": "top-right", "size": 0.25, "duration": 30000, "mute": False})


class SelectTests(unittest.IsolatedAsyncioTestCase):
    async def test_options_are_off_plus_the_configured_cameras_by_friendly_name(self):
        s, _ = select({"pip_cameras": ["camera.door", "camera.yard"]}, {"camera.door": "Doorbell"})
        self.assertEqual(s.options, [OFF, "Doorbell", "camera.yard"])

    async def test_duplicate_names_stay_selectable(self):
        s, _ = select({"pip_cameras": ["camera.a", "camera.b"]}, {"camera.a": "Cam", "camera.b": "Cam"})
        self.assertEqual(s.options, [OFF, "Cam (camera.a)", "Cam (camera.b)"])

    async def test_selecting_a_camera_shows_it_with_the_option_defaults(self):
        s, coord = select({"pip_cameras": ["camera.door"], "pip_position": "bottom", "pip_duration": 60}, {"camera.door": "Doorbell"})
        await s.async_select_option("Doorbell")
        self.assertEqual(coord.pip.shown, [("camera.door", {"position": "bottom", "size": 0.4, "duration": 60000, "mute": True})])

    async def test_selecting_another_camera_replaces_the_first(self):
        s, coord = select({"pip_cameras": ["camera.a", "camera.b"]})
        await s.async_select_option("camera.a")
        await s.async_select_option("camera.b")
        self.assertEqual([c for c, _ in coord.pip.shown], ["camera.a", "camera.b"])

    async def test_off_hides(self):
        s, coord = select()
        await s.async_select_option(OFF)
        self.assertEqual(coord.pip.hid, 1)

    async def test_unknown_option_is_refused(self):
        s, _ = select()
        with self.assertRaises(HomeAssistantError):
            await s.async_select_option("Nonsense")

    async def test_current_option_follows_the_pip_state(self):
        s, coord = select({"pip_cameras": ["camera.door"]}, pip={"state": "playing", "entity_id": "camera.door"})
        self.assertEqual(s.current_option, "camera.door")
        coord.on_state("pip", {"state": "hidden", "entity_id": ""})
        self.assertEqual(s.current_option, OFF)
        coord.on_state("pip", {"state": "error", "entity_id": "camera.door"})
        self.assertEqual(s.current_option, OFF)

    async def test_a_camera_shown_by_the_action_that_is_not_in_the_list_reads_as_off(self):
        s, coord = select({"pip_cameras": ["camera.door"]}, pip={"state": "playing", "entity_id": "camera.other"})
        self.assertEqual(s.current_option, OFF)

    async def test_show_action_accepts_any_camera_with_overrides(self):
        s, coord = select()
        await s.async_show_pip(camera="camera.any", position="top-left", size="small", duration=0, mute=False)
        self.assertEqual(coord.pip.shown, [("camera.any", {"position": "top-left", "size": 0.25, "duration": 0, "mute": False})])

    async def test_errors_reach_the_user(self):
        s, coord = select()
        from custom_components.decky_home_assistant.client import DeviceOffline
        coord.pip.error = DeviceOffline("x")
        with self.assertRaises(HomeAssistantError) as cm:
            await s.async_show_pip(camera="camera.a")
        self.assertIn("offline", str(cm.exception))
        coord.pip.error = CommandError("unsupported", "this only works in game mode")
        with self.assertRaises(HomeAssistantError) as cm:
            await s.async_hide_pip()
        self.assertIn("game mode", str(cm.exception))

    async def test_services_are_registered(self):
        coord = make_coordinator()
        connect(coord)
        entry = ConfigEntry()
        entry.runtime_data = coord
        await select_platform.async_setup_entry(coord.hass, entry, Collector())
        schema, func = PLATFORM.services["show_pip"]
        self.assertEqual((func, {k.key for k in schema}), ("async_show_pip", {"camera", "position", "size", "duration", "mute"}))
        self.assertEqual(PLATFORM.services["hide_pip"][1], "async_hide_pip")

    async def test_unavailable_when_the_device_is_offline(self):
        s, coord = select()
        coord.on_connection(False)
        self.assertFalse(s.available)


class PipStateSensorTests(unittest.IsolatedAsyncioTestCase):
    def sensor(self, **groups):
        coord = make_coordinator()
        connect(coord, **groups)
        return sensor.PipStateSensor(coord), coord

    async def test_hidden_is_idle_and_states_pass_through(self):
        s, coord = self.sensor(pip={"state": "hidden", "entity_id": "", "message": ""})
        self.assertEqual(s.native_value, "idle")
        for state in ("loading", "connecting", "negotiating", "playing", "error"):
            coord.on_state("pip", {"state": state, "entity_id": "camera.a", "message": "m"})
            self.assertEqual(s.native_value, state)
        self.assertEqual(s.extra_state_attributes, {"camera": "camera.a", "message": "m"})

    async def test_unknown_state_is_error_and_offline_is_unavailable(self):
        s, coord = self.sensor(pip={"state": "weird"})
        self.assertEqual(s.native_value, "error")
        coord.on_connection(False)
        self.assertFalse(s.available)

    async def test_auto_hide_returns_to_idle(self):
        s, coord = self.sensor(pip={"state": "playing", "entity_id": "camera.a"})
        coord.on_state("pip", {"state": "hidden", "entity_id": "", "message": ""})
        self.assertEqual(s.native_value, "idle")
