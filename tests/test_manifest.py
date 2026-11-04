import json
import os
import unittest

from . import ROOT

DIR = os.path.join(ROOT, "custom_components", "piston")


def _load(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return json.load(f)


class ManifestTests(unittest.TestCase):
    def test_required_keys(self):
        m = _load("custom_components", "piston", "manifest.json")
        for key in ("domain", "name", "version", "config_flow", "iot_class", "documentation", "codeowners"):
            self.assertIn(key, m)
        self.assertEqual(m["domain"], "piston")
        self.assertTrue(m["config_flow"])
        self.assertEqual(m["iot_class"], "local_push")

    def test_version_matches_package_json(self):
        self.assertEqual(
            _load("custom_components", "piston", "manifest.json")["version"],
            _load("package.json")["version"],
        )

    def test_hacs_json(self):
        h = _load("hacs.json")
        self.assertIn("name", h)

    def test_translations_valid(self):
        self.assertIn("config", _load("custom_components", "piston", "translations", "en.json"))
