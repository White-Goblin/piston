import asyncio
import json
import os
import re
import stat
import unittest

from . import use_temp_settings_dir
from decky_ha.settings import SettingsMixin, clean_apps, default_settings


class Settings(SettingsMixin):
    pass


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = use_temp_settings_dir()
        self.path = os.path.join(self.dir, "settings.json")
        self.s = Settings()

    def test_first_run_creates_stable_device_id(self):
        first = self.s._load_settings()["device_id"]
        self.assertRegex(first, r"^[a-z0-9]{12}$")
        self.assertEqual(Settings()._load_settings()["device_id"], first)
        with open(self.path) as f:
            self.assertEqual(json.load(f)["device_id"], first)

    def test_unknown_keys_are_dropped(self):
        with open(self.path, "w") as f:
            json.dump({"unexpected": "x", "another_setting": True, "device_id": "abcdef012345", "apps": []}, f)
        loaded = self.s._load_settings()
        self.assertEqual(set(loaded), set(default_settings()))
        self.assertEqual(loaded["device_id"], "abcdef012345")
        with open(self.path) as f:
            self.assertEqual(set(json.load(f)), set(default_settings()))

    def test_file_mode_is_600(self):
        self.s._update_settings({"secret": "aa"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_get_settings_never_returns_the_secret(self):
        self.s._update_settings({"secret": "deadbeef"})
        result = asyncio.run(self.s.get_settings())
        self.assertNotIn("secret", result)
        self.assertNotIn("deadbeef", json.dumps(result))
        self.assertTrue(result["paired"])

    def test_unpaired_by_default(self):
        self.assertFalse(self.s._is_paired())
        self.assertFalse(asyncio.run(self.s.get_settings())["paired"])

    def test_corrupt_file_falls_back_to_defaults(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertRegex(self.s._load_settings()["device_id"], r"^[a-z0-9]{12}$")

    def test_save_apps_validates(self):
        saved = asyncio.run(self.s.save_apps([
            {"appid": "250900", "name": "Isaac", "kind": "steam"},
            {"appid": "250900", "name": "Dup"},
            {"appid": "2451505133", "name": "Jellyfin", "kind": "shortcut"},
            {"appid": "bad", "name": "x"}, "junk", {"appid": "5", "kind": "weird"},
        ]))
        self.assertEqual([a["appid"] for a in saved], ["250900", "2451505133", "5"])
        self.assertEqual(saved[2]["kind"], "steam")
        self.assertEqual(self.s._load_settings()["apps"], saved)

    def test_app_cap(self):
        apps = clean_apps([{"appid": str(i)} for i in range(100)])
        self.assertEqual(len(apps), 25)

    def test_atomic_write_leaves_no_temp_files(self):
        self.s._update_settings({"device_name": "x"})
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])
        self.assertFalse(re.search(r"tmp", " ".join(os.listdir(self.dir))))
