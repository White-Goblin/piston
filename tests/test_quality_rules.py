"""Quality-scale rules that can be checked without Home Assistant: platform limits, action exceptions, field descriptions."""
import importlib
import json
import os
import unittest

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.decky_home_assistant import const
from custom_components.decky_home_assistant.client import CommandError, DeviceOffline
from custom_components.decky_home_assistant.entity import command_failure
from custom_components.decky_home_assistant.media_player import DeckyMediaPlayer
from custom_components.decky_home_assistant.proto import messages as m
from custom_components.decky_home_assistant.select import PipCameraSelect
from .entityutil import connect, make_coordinator

PACKAGE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "decky_home_assistant")
READ_ONLY = {"binary_sensor", "sensor", "event"}


class ParallelUpdatesTests(unittest.TestCase):
    def test_every_platform_sets_it_explicitly(self):
        for platform in const.PLATFORMS:
            module = importlib.import_module(f"custom_components.decky_home_assistant.{platform}")
            self.assertTrue(hasattr(module, "PARALLEL_UPDATES"), platform)

    def test_read_only_platforms_are_unlimited_and_the_media_player_must_not_serialize_launches(self):
        for platform in const.PLATFORMS:
            module = importlib.import_module(f"custom_components.decky_home_assistant.{platform}")
            if platform in READ_ONLY or platform == "media_player":
                self.assertEqual(module.PARALLEL_UPDATES, 0, platform)   # media player: the device keeps last-command-wins
            else:
                self.assertEqual(module.PARALLEL_UPDATES, 1, platform)


class ActionExceptionTests(unittest.IsolatedAsyncioTestCase):
    def test_wrong_input_is_a_validation_error_and_failures_are_not(self):
        bad = command_failure(CommandError(m.BAD_REQUEST, "message is required"))
        self.assertIsInstance(bad, ServiceValidationError)
        self.assertEqual(str(bad), "message is required")
        for failure in (CommandError(m.INTERNAL, "the device did not answer in time"), CommandError(m.UNSUPPORTED, "game mode only"),
                        CommandError(m.NOT_RUNNING, "")):
            error = command_failure(failure)
            self.assertIsInstance(error, HomeAssistantError, failure.code)
            self.assertNotIsInstance(error, ServiceValidationError, failure.code)
        offline = command_failure(DeviceOffline("gone"))
        self.assertEqual((type(offline), str(offline)), (HomeAssistantError, "The device is offline"))

    async def test_an_unknown_source_is_a_validation_error(self):
        coord = make_coordinator()
        connect(coord, apps={"apps": [{"appid": "1", "name": "A"}], "running": False})
        with self.assertRaises(ServiceValidationError):
            await DeckyMediaPlayer(coord).async_select_source("Nope")

    async def test_turn_on_without_a_known_mac_is_a_validation_error(self):
        coord = make_coordinator()
        with self.assertRaises(ServiceValidationError):
            await DeckyMediaPlayer(coord).async_turn_on()

    async def test_an_option_that_is_not_a_pip_camera_is_a_validation_error(self):
        coord = make_coordinator(options={"pip_cameras": ["camera.door"]})
        connect(coord, pip={"state": "hidden"})
        with self.assertRaises(ServiceValidationError):
            await PipCameraSelect(coord).async_select_option("camera.nowhere")

    async def test_a_bad_request_from_the_device_reaches_the_caller_as_a_validation_error(self):
        coord = make_coordinator()
        connect(coord)
        coord.client.error = CommandError(m.BAD_REQUEST, "message is required")
        from custom_components.decky_home_assistant.notify import DeckyNotify
        with self.assertRaises(ServiceValidationError):
            await DeckyNotify(coord).async_show_toast(message=" ")
        coord.client.error = CommandError(m.INTERNAL, "boom")
        with self.assertRaises(HomeAssistantError) as raised:
            await DeckyNotify(coord).async_show_toast(message="hi")
        self.assertNotIsInstance(raised.exception, ServiceValidationError)


class FieldDescriptionTests(unittest.TestCase):
    """The config-flow rule: every field of every step has a label ("data") and an explanation ("data_description")."""

    def steps(self, name):
        with open(os.path.join(PACKAGE, name)) as f:
            tree = json.load(f)
        for flow in ("config", "options"):
            for step_id, step in tree.get(flow, {}).get("step", {}).items():
                yield f"{flow}.{step_id}", step

    def test_every_field_is_described_in_strings_and_translations(self):
        for filename in ("strings.json", "translations/en.json"):
            for where, step in self.steps(filename):
                fields = set(step.get("data", {}))
                self.assertEqual(fields - set(step.get("data_description", {})), set(), f"{filename} {where}")

    def test_strings_and_the_english_translation_agree(self):
        with open(os.path.join(PACKAGE, "strings.json")) as a, open(os.path.join(PACKAGE, "translations", "en.json")) as b:
            self.assertEqual(json.load(a), json.load(b))


if __name__ == "__main__":
    unittest.main()
