"""Which entities are available in which device state: the media player stays available while the device is asleep or off
(turn_on must work), everything else is available only while the device is online, and a problem takes all of them away."""
import unittest

from custom_components.decky_home_assistant.binary_sensor import ConnectivitySensor
from custom_components.decky_home_assistant.button import DeckyPowerButton
from custom_components.decky_home_assistant.client import AuthFailed, VersionSkew
from custom_components.decky_home_assistant.event import ToastClickEvent
from custom_components.decky_home_assistant.media_player import DeckyMediaPlayer
from custom_components.decky_home_assistant.notify import DeckyNotify
from custom_components.decky_home_assistant.select import PipCameraSelect
from custom_components.decky_home_assistant.sensor import DeckySensor, PipStateSensor, SteamScreenSensor
from custom_components.decky_home_assistant.switch import DesktopModeSwitch
from .entityutil import connect, make_coordinator

GROUPS = {"device": {"session": "game"}, "apps": {"screen": "game", "running": False}, "pip": {"state": "hidden"},
          "sensors": {"cpu_load": 3}}


def entities(coord):
    """One of every kind of entity that follows the 'online' rule (the media player and connectivity are special)."""
    return {
        "switch": DesktopModeSwitch(coord), "button": DeckyPowerButton(coord, "restart_device", "restart"),
        "sensor": DeckySensor(coord, "cpu_load"), "steam screen": SteamScreenSensor(coord), "pip state": PipStateSensor(coord),
        "notify": DeckyNotify(coord), "toast event": ToastClickEvent(coord), "pip select": PipCameraSelect(coord),
    }


class AvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def awake(self):
        coord = make_coordinator()
        connect(coord, **GROUPS)
        return coord

    def assert_others(self, coord, expected):
        for name, entity in entities(coord).items():
            self.assertEqual(entity.available, expected, name)

    async def test_connected_and_awake_everything_is_available(self):
        coord = self.awake()
        self.assertTrue(DeckyMediaPlayer(coord).available)
        self.assert_others(coord, True)

    async def test_off_media_player_available_with_state_off_everything_else_unavailable(self):
        coord = make_coordinator()                                   # never connected: off
        player = DeckyMediaPlayer(coord)
        self.assertEqual((player.available, str(player.state)), (True, "off"))
        self.assert_others(coord, False)

    async def test_standby_media_player_available_everything_else_unavailable(self):
        coord = self.awake()
        coord.on_event("going_to_sleep", {})
        player = DeckyMediaPlayer(coord)
        self.assertEqual((player.available, str(player.state)), (True, "standby"))
        self.assert_others(coord, False)

    async def test_a_shutdown_is_the_same_as_off(self):
        coord = self.awake()
        coord.on_event("shutting_down", {"reason": "shutdown"})
        self.assertEqual((DeckyMediaPlayer(coord).available, str(DeckyMediaPlayer(coord).state)), (True, "off"))
        self.assert_others(coord, False)

    async def test_an_authentication_problem_makes_everything_unavailable_including_the_media_player(self):
        coord = make_coordinator()
        coord.on_problem(AuthFailed("rejected"))
        self.assertEqual(coord.problem, "auth_failed")
        self.assertFalse(DeckyMediaPlayer(coord).available)
        self.assert_others(coord, False)

    async def test_a_version_problem_makes_everything_unavailable_including_the_media_player(self):
        for peer in ("old", "new"):
            coord = make_coordinator()
            coord.on_problem(VersionSkew(peer))
            self.assertEqual(coord.problem, f"version_skew_{peer}")
            self.assertFalse(DeckyMediaPlayer(coord).available, peer)
            self.assert_others(coord, False)

    async def test_a_problem_wins_even_if_a_connection_somehow_exists(self):
        coord = self.awake()
        coord.problem = "auth_failed"
        self.assertFalse(coord.online)
        self.assertFalse(DeckyMediaPlayer(coord).available)
        self.assert_others(coord, False)

    async def test_everything_recovers_when_the_device_connects_again(self):
        coord = make_coordinator()
        coord.on_problem(VersionSkew("old"))
        connect(coord, **GROUPS)                                      # updated, connects: the problem clears
        self.assertIsNone(coord.problem)
        self.assertTrue(DeckyMediaPlayer(coord).available)
        self.assert_others(coord, True)

    async def test_connectivity_stays_available_to_say_off(self):
        coord = make_coordinator()
        sensor = ConnectivitySensor(coord)
        self.assertEqual((sensor.available, sensor.is_on), (True, False))


if __name__ == "__main__":
    unittest.main()
