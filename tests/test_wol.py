import unittest
from unittest import mock

from homeassistant.exceptions import HomeAssistantError

from custom_components.decky_home_assistant import wol
from custom_components.decky_home_assistant.media_player import DeckyMediaPlayer
from .entityutil import connect, make_coordinator


class MagicPacketTests(unittest.TestCase):
    def test_bytes(self):
        packet = wol.magic_packet("AA:BB:CC:DD:EE:FF")
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:12], bytes.fromhex("aabbccddeeff"))
        self.assertEqual(packet[6:], bytes.fromhex("aabbccddeeff") * 16)

    def test_dash_separated_mac(self):
        self.assertEqual(wol.magic_packet("aa-bb-cc-dd-ee-ff"), wol.magic_packet("aa:bb:cc:dd:ee:ff"))

    def test_bad_macs(self):
        for bad in ("", "zz:zz:zz:zz:zz:zz", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:ff:00"):
            with self.assertRaises(ValueError):
                wol.magic_packet(bad)


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_to_both_broadcasts_on_both_ports(self):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        with mock.patch.object(wol.socket, "socket", return_value=sock):
            await wol.send_magic_packet("aa:bb:cc:dd:ee:ff", "192.168.1.255")
        sent = sorted((c.args[1]) for c in sock.sendto.call_args_list)
        self.assertEqual(sent, [("192.168.1.255", 7), ("192.168.1.255", 9), ("255.255.255.255", 7), ("255.255.255.255", 9)])
        self.assertTrue(all(c.args[0] == wol.magic_packet("aa:bb:cc:dd:ee:ff") for c in sock.sendto.call_args_list))

    async def test_duplicate_broadcast_is_sent_once(self):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        with mock.patch.object(wol.socket, "socket", return_value=sock):
            await wol.send_magic_packet("aa:bb:cc:dd:ee:ff", "255.255.255.255")
        self.assertEqual(sock.sendto.call_count, 2)


class TurnOnTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_on_from_standby_sends_the_packet(self):
        coord = make_coordinator()
        coord.on_state("device", {"mac": "aa:bb:cc:dd:ee:ff", "broadcast": "10.0.0.255"})
        coord.on_event("going_to_sleep", {})
        coord.on_connection(False)
        with mock.patch("custom_components.decky_home_assistant.media_player.send_magic_packet", mock.AsyncMock()) as send:
            await DeckyMediaPlayer(coord).async_turn_on()
        send.assert_awaited_once_with("aa:bb:cc:dd:ee:ff", "10.0.0.255")

    async def test_uses_the_stored_mac_when_the_device_never_connected_this_run(self):
        coord = make_coordinator(data={"mac": "11:22:33:44:55:66"})
        with mock.patch("custom_components.decky_home_assistant.media_player.send_magic_packet", mock.AsyncMock()) as send:
            await DeckyMediaPlayer(coord).async_turn_on()
        send.assert_awaited_once_with("11:22:33:44:55:66", "255.255.255.255")

    async def test_turn_on_wakes_a_suspended_device_whose_connection_has_not_dropped_yet(self):
        coord = make_coordinator()
        connect(coord, device={"mac": "aa:bb:cc:dd:ee:ff", "broadcast": "10.0.0.255"})
        coord.on_event("going_to_sleep", {})  # connected is still True, the device is asleep
        self.assertTrue(coord.connected)
        with mock.patch("custom_components.decky_home_assistant.media_player.send_magic_packet", mock.AsyncMock()) as send:
            await DeckyMediaPlayer(coord).async_turn_on()
        send.assert_awaited_once_with("aa:bb:cc:dd:ee:ff", "10.0.0.255")

    async def test_turn_on_while_connected_does_nothing(self):
        coord = make_coordinator()
        connect(coord, device={"mac": "aa:bb:cc:dd:ee:ff"})
        with mock.patch("custom_components.decky_home_assistant.media_player.send_magic_packet", mock.AsyncMock()) as send:
            await DeckyMediaPlayer(coord).async_turn_on()
        send.assert_not_awaited()

    async def test_unknown_mac_is_a_clear_error(self):
        with self.assertRaises(HomeAssistantError) as cm:
            await DeckyMediaPlayer(make_coordinator()).async_turn_on()
        self.assertIn("MAC", str(cm.exception))

    async def test_socket_errors_become_user_errors(self):
        coord = make_coordinator(data={"mac": "11:22:33:44:55:66"})
        with mock.patch("custom_components.decky_home_assistant.media_player.send_magic_packet",
                        mock.AsyncMock(side_effect=OSError("network unreachable"))):
            with self.assertRaises(HomeAssistantError):
                await DeckyMediaPlayer(coord).async_turn_on()

    async def test_feature_is_advertised(self):
        from homeassistant.components.media_player import MediaPlayerEntityFeature as F
        self.assertTrue(DeckyMediaPlayer(make_coordinator()).supported_features & F.TURN_ON)
