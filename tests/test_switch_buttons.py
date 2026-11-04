import unittest

from homeassistant.exceptions import HomeAssistantError

from custom_components.piston.button import BUTTONS, DeckyPowerButton
from custom_components.piston.switch import DesktopModeSwitch
from .entityutil import connect, make_coordinator


class SwitchTests(unittest.IsolatedAsyncioTestCase):
    def sw(self, session="game"):
        coord = make_coordinator()
        connect(coord, device={"session": session})
        return DesktopModeSwitch(coord), coord

    async def test_state_follows_the_reported_session(self):
        sw, coord = self.sw("game")
        self.assertEqual((sw.is_on, sw.available), (False, True))
        coord.on_state("device", {"session": "desktop"})
        self.assertEqual((sw.is_on, sw.available), (True, True))

    async def test_turn_on_and_off_send_the_session_commands(self):
        sw, coord = self.sw("game")
        await sw.async_turn_on()
        coord.on_state("device", {"session": "desktop"})
        await sw.async_turn_off()
        self.assertEqual(coord.client.calls, [("power", {"action": "desktop"}), ("power", {"action": "game"})])

    async def test_unavailable_when_offline_or_session_unknown(self):
        sw, coord = self.sw("unknown")
        self.assertFalse(sw.available)
        coord.on_state("device", {"session": "game"})
        coord.on_connection(False)
        self.assertFalse(sw.available)

    async def test_offline_command_is_a_user_error(self):
        sw, coord = self.sw()
        coord.client.connected = False
        with self.assertRaises(HomeAssistantError):
            await sw.async_turn_off()


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_buttons_send_the_right_actions_and_follow_the_connection(self):
        coord = make_coordinator()
        connect(coord)
        buttons = [DeckyPowerButton(coord, key, action) for key, action in BUTTONS]
        for b in buttons:
            await b.async_press()
        self.assertEqual([c[1]["action"] for c in coord.client.calls], ["restart", "restart_steam"])
        self.assertEqual([b.unique_id for b in buttons], ["dev123abc456_restart_device", "dev123abc456_restart_steam"])
        coord.on_connection(False)
        self.assertFalse(any(b.available for b in buttons))

    async def test_pressing_while_offline_is_a_user_error(self):
        coord = make_coordinator()
        connect(coord)
        coord.client.connected = False
        with self.assertRaises(HomeAssistantError):
            await DeckyPowerButton(coord, "restart_steam", "restart_steam").async_press()
