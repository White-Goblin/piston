"""The handshake is frozen from the update lane on: a change here needs the plugin installed by hand again."""
import json
import os
import tempfile
import unittest

from decky_ha import version as version_module
from decky_ha.proto import LANE_VERSION, auth, messages as m

from . import ROOT


class FrozenHandshake(unittest.TestCase):
    def test_the_ha_hello_keys(self):
        self.assertEqual(sorted(m.hello("ha")), ["device_id", "min_proto", "proto", "type"])

    def test_the_device_hello_keys(self):
        # the server adds `paired` and the main module adds `name`; the lane adds the two below
        frame = {**m.hello("dev"), "paired": True, "name": "x", **m.device_hello_extra("1.2.3", LANE_VERSION)}
        self.assertEqual(sorted(frame), ["device_id", "lane", "min_proto", "name", "paired", "plugin_version", "proto", "type"])
        self.assertEqual(m.parse(m.encode(frame))["plugin_version"], "1.2.3")
        self.assertEqual(LANE_VERSION, 1)

    def test_the_auth_frames(self):
        nonce = auth.make_challenge()
        proof = auth.prove(b"k" * 32, nonce, nonce, auth.ROLE_HA)
        self.assertTrue(auth.verify(b"k" * 32, nonce, nonce, auth.ROLE_HA, proof))
        # the proof input carries no protocol number: the same inputs verify whatever versions the sides speak
        self.assertEqual(proof, auth.prove(b"k" * 32, nonce, nonce, auth.ROLE_HA))

    def test_the_close_codes(self):
        from decky_ha import server
        self.assertEqual((server.CLOSE_VERSION, server.CLOSE_REPLACED, server.CLOSE_UNAUTHORIZED, server.CLOSE_BAD_REQUEST),
                         (4000, 4001, 4002, 4003))

    def test_a_malformed_plugin_version_or_lane_is_rejected(self):
        for extra in ({"plugin_version": "1.2"}, {"plugin_version": 3}, {"plugin_version": "1.2.3-beta"}, {"lane": "1"}, {"lane": -1},
                      {"lane": True}):
            with self.assertRaises(m.ProtocolError, msg=str(extra)):
                m.parse(json.dumps({**m.hello("d"), **extra}))


class PluginVersion(unittest.TestCase):
    def test_reads_package_json(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "package.json"), "w") as f:
                json.dump({"version": "0.4.0"}, f)
            self.assertEqual(version_module.plugin_version(root), "0.4.0")

    def test_missing_or_bad_file_gives_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(version_module.plugin_version(root))
            with open(os.path.join(root, "package.json"), "w") as f:
                f.write('{"version": "dev"}')
            self.assertIsNone(version_module.plugin_version(root))

    def test_the_shipped_package_json_has_a_valid_version(self):
        self.assertRegex(version_module.plugin_version(ROOT) or "", r"^\d+\.\d+\.\d+$")
