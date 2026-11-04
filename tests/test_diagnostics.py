import json
import re
import unittest
from datetime import datetime, timezone

from custom_components.piston import diagnostics
from custom_components.piston.diagnostics import TO_REDACT, async_get_config_entry_diagnostics
from .entityutil import connect, make_coordinator

SECRET = "ab" * 32
HOST = "192.168.1.5"
MAC = "aa:bb:cc:dd:ee:ff"
BROADCAST = "192.168.1.255"
SERIAL = "SERIAL-0042"
NETWORK = "Grandma's Wi-Fi"


def populated():
    coord = make_coordinator(options={"sensor_interval": 30}, data={"host": HOST, "secret": SECRET, "mac": MAC})
    coord.entry.runtime_data = coord
    connect(coord, device={
        "device_id": "dev123abc456", "name": "Box", "mac": MAC, "broadcast": BROADCAST, "protocol": 2, "session": "game",
        "wol": {"interface": "wlan0", "connection": NETWORK, "mac": MAC, "broadcast": BROADCAST, "supported": True,
                "error": f"nmcli failed talking to {HOST} ({MAC})"}},
        sensors={"cpu_load": 3, "ip": HOST},
        controllers={"connected": {SERIAL: {"battery": 50}}, "seen": [{"serial": SERIAL, "name": "Pad"}]})
    return coord


class SendspinDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_sendspin_status_is_included_and_addresses_in_its_error_are_removed(self):
        coord = populated()
        coord.on_state("sendspin", {"enabled": True, "status": "error", "name": "Box", "port": 8928, "server_name": "Music Assistant",
                                    "error": f"cannot reach {HOST}"})
        report = await async_get_config_entry_diagnostics(None, coord.entry)
        group = report["groups"]["sendspin"]
        self.assertEqual((group["status"], group["port"], group["server_name"]), ("error", 8928, "Music Assistant"))
        self.assertNotIn(HOST, json.dumps(report))
        self.assertIn("**IP**", group["error"])

    async def test_an_older_device_has_no_sendspin_section(self):
        report = await async_get_config_entry_diagnostics(None, populated().entry)
        self.assertNotIn("sendspin", report["groups"])


class MediaDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_media_group_shows_its_shape_but_not_what_is_watched(self):
        coord = populated()
        coord.on_state("media", {"title": "Secret Show", "artist": "Somebody", "album": "Season 9", "kind": "video",
                                 "source": "mpris", "duration": 600.0, "position": 5.0, "position_at": 1.0, "art": "0123456789abcdef"})
        report = await async_get_config_entry_diagnostics(coord.hass, coord.entry)
        text = json.dumps(report)
        for private in ("Secret Show", "Somebody", "Season 9", "0123456789abcdef"):
            self.assertNotIn(private, text)
        media = report["groups"]["media"]
        self.assertEqual((media["title"], media["kind"], media["source"], media["has_art"], media["duration"]),
                         ("**REDACTED**", "video", "mpris", True, 600.0))
        self.assertNotIn("art", media)

    async def test_no_media_group_means_none_in_the_report(self):
        report = await async_get_config_entry_diagnostics(populated().hass, populated().entry)
        self.assertNotIn("media", report["groups"])


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def report(self, coord):
        return await async_get_config_entry_diagnostics(coord.hass, coord.entry)

    async def test_no_secret_address_mac_serial_or_network_name_appears_anywhere(self):
        text = json.dumps(await self.report(populated()))
        for private in (SECRET, HOST, MAC, BROADCAST, SERIAL, NETWORK, "192.168.1"):
            self.assertNotIn(private, text, private)
        self.assertIsNone(re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text))
        self.assertIsNone(re.search(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", text, re.I))

    async def test_what_support_needs_is_still_there(self):
        report = await self.report(populated())
        self.assertEqual(report["entry"]["unique_id"], "dev123abc456")
        self.assertEqual(report["entry"]["options"], {"sensor_interval": 30})
        self.assertEqual(report["entry"]["data"]["secret"], "**REDACTED**")
        self.assertEqual(report["entry"]["data"]["device_id"], "dev123abc456")
        device = report["groups"]["device"]
        self.assertEqual((device["device_id"], device["protocol"], device["session"], device["wol"]["interface"]),
                         ("dev123abc456", 2, "game", "wlan0"))
        self.assertEqual(report["groups"]["sensors"]["cpu_load"], 3)
        coordinator = report["coordinator"]
        self.assertEqual((coordinator["power"], coordinator["connected"], coordinator["problem"], coordinator["device_protocol"]),
                         ("on", True, None, 2))

    async def test_an_offline_device_still_reports_its_last_known_state(self):
        coord = populated()
        coord.on_connection(False)
        report = await self.report(coord)
        self.assertEqual(report["coordinator"]["connected"], False)
        self.assertEqual(report["groups"]["device"]["name"], "Box")      # last known
        self.assertEqual(report["groups"]["sensors"]["cpu_load"], 3)
        self.assertIsNotNone(report["coordinator"]["last_connected"])

    async def test_it_is_json_serializable_with_restored_groups_and_timestamps(self):
        coord = populated()
        coord.restore({"audio": {"mode": "cec"}}, datetime(2026, 11, 1, tzinfo=timezone.utc))
        report = await self.report(coord)
        self.assertEqual(report["coordinator"]["restored_groups"], ["audio"])
        json.dumps(report)

    async def test_a_problem_state_is_reported(self):
        coord = make_coordinator()
        coord.entry.runtime_data = coord
        coord.problem = "version_skew_old"
        report = await self.report(coord)
        self.assertEqual(report["coordinator"]["problem"], "version_skew_old")
        self.assertEqual(report["groups"], {})

    async def test_connected_controllers_are_numbered_not_named_by_serial(self):
        report = await self.report(populated())
        self.assertEqual(list(report["groups"]["controllers"]["connected"]), ["controller_1"])
        self.assertEqual(report["groups"]["controllers"]["connected"]["controller_1"]["battery"], 50)

    async def test_the_live_state_is_not_modified_by_building_the_report(self):
        coord = populated()
        await self.report(coord)
        self.assertEqual(list(coord.groups["controllers"]["connected"]), [SERIAL])

    async def test_the_redaction_list_is_fixed(self):
        self.assertEqual(TO_REDACT, {"secret", "host", "mac", "broadcast", "ip", "serial", "connection", "ssid"})

    async def test_text_with_addresses_is_scrubbed_even_under_other_keys(self):
        self.assertEqual(diagnostics._plain("failed at 10.0.0.2 / aa-bb-cc-dd-ee-ff"), "failed at **IP** / **MAC**")
        self.assertEqual(diagnostics._plain("version 2026.10.4"), "version 2026.10.4")


if __name__ == "__main__":
    unittest.main()


class UpdateDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_update_block_shows_versions_and_the_last_failure_but_no_addresses(self):
        coord = populated()
        coord.target_version, coord.plugin_version = "0.4.1", "0.4.0"
        coord.update_latch = {"version": "0.4.1", "reason": "hash"}
        report = await async_get_config_entry_diagnostics(coord.hass, coord.entry)
        self.assertEqual(report["update"], {"plugin_version": "0.4.0", "target_version": "0.4.1", "auto_update": True,
                                            "state": "failed", "skewed": None, "last_failure": {"version": "0.4.1", "reason": "hash"}})
