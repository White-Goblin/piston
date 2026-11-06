import json
import logging
import os
import unittest

from homeassistant.core import HomeAssistant

from custom_components.piston import card
from . import ROOT


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_card_is_served_and_added_once_with_a_versioned_url(self):
        hass = HomeAssistant()
        await card.async_register_card(hass, "1.2.3")
        await card.async_register_card(hass, "1.2.3")                 # a second entry: nothing again
        (config,) = hass.http.static_paths
        self.assertEqual((config.url_path, config.cache_headers), ("/piston_static", False))
        self.assertTrue(os.path.isfile(os.path.join(config.path, "piston-remote-card.js")))
        (url,) = hass.data["extra_js_urls"]
        self.assertRegex(url, r"^/piston_static/piston-remote-card\.js\?v=1\.2\.3-[0-9a-f]{8}$")

    async def test_without_a_known_version_the_url_has_no_query(self):
        self.assertEqual(card.card_url(None), "/piston_static/piston-remote-card.js")
        self.assertEqual(card.card_url("0.8.0"), "/piston_static/piston-remote-card.js?v=0.8.0")
        self.assertEqual(card.card_url("0.8.0", "abc12345"), "/piston_static/piston-remote-card.js?v=0.8.0-abc12345")
        self.assertEqual(card.card_url(None, "abc12345"), "/piston_static/piston-remote-card.js?v=abc12345")

    async def test_the_address_changes_when_the_card_file_changes(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / card.CARD_FILE).write_text("one")
            with mock.patch.object(card, "WWW_DIR", Path(d)):
                first = card._content_tag()
                (Path(d) / card.CARD_FILE).write_text("two")
                second = card._content_tag()
        self.assertNotEqual(first, second)

    async def test_a_failing_registration_only_logs_a_warning(self):
        hass = HomeAssistant()

        async def boom(configs):
            raise RuntimeError("no http")

        hass.http.async_register_static_paths = boom
        with self.assertLogs("custom_components.piston.card", logging.WARNING) as logs:
            await card.async_register_card(hass, "1.2.3")             # must not raise
        self.assertIn("remote card", logs.output[0])
        self.assertNotIn("extra_js_urls", hass.data)

    async def test_a_hass_without_http_is_survived(self):
        hass = HomeAssistant()
        del hass.http
        with self.assertLogs("custom_components.piston.card", logging.WARNING):
            await card.async_register_card(hass, None)


class PackagingTests(unittest.TestCase):
    def test_the_manifest_declares_the_components_the_card_needs(self):
        with open(os.path.join(ROOT, "custom_components", "piston", "manifest.json")) as f:
            manifest = json.load(f)
        self.assertTrue({"frontend", "http", "websocket_api"} <= set(manifest["dependencies"]))

    def test_the_card_file_is_inside_the_integration_folder(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "custom_components", "piston", "www", "piston-remote-card.js")))
