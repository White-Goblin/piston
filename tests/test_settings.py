import asyncio
import json
import os
import re
import stat
import unittest

from . import use_temp_settings_dir
from piston_device.settings import SettingsMixin, clean_apps, clean_sendspin, default_settings


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


class SendspinSettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = use_temp_settings_dir()
        self.path = os.path.join(self.dir, "settings.json")
        self.s = Settings()

    def test_defaults_and_old_files(self):
        with open(self.path, "w") as f:
            json.dump({"device_id": "abcdef012345", "secret": "aa", "apps": []}, f)  # a file from before Sendspin
        loaded = self.s._load_settings()
        self.assertEqual((loaded["sendspin_enabled"], loaded["sendspin_volume"], loaded["sendspin_muted"], loaded["sendspin_output_delay_ms"]),
                         (True, 50, False, 0))
        self.assertEqual((loaded["sendspin_name"], loaded["sendspin_private_key"], loaded["secret"]), ("", "", "aa"))

    def test_the_default_name_is_the_device_name_with_audio_added_until_one_is_chosen(self):
        self.s._update_settings({"device_name": "Living room"})
        self.assertEqual(self.s._sendspin_name(), "Living room audio")
        self.s._save_sendspin(sendspin_name="  Big screen ")
        self.assertEqual(self.s._sendspin_name(), "Big screen")
        self.s._save_sendspin(sendspin_name="")
        self.assertEqual(self.s._sendspin_name(), "Living room audio")

    def test_a_long_chosen_name_is_cut_to_64_bytes_on_a_character_boundary(self):
        self.s._save_sendspin(sendspin_name="é" * 40)
        name = self.s._sendspin_name()
        self.assertEqual(len(name.encode()), 64)
        self.assertEqual(name, "é" * 32)
        self.assertEqual(len(clean_sendspin({**default_settings(), "sendspin_name": "日" * 30})["sendspin_name"].encode()), 63)

    def test_the_default_name_still_ends_with_audio_when_the_device_name_is_long(self):
        for device_name in ("x" * 100, "é" * 60, "日" * 40, "Box " + "y" * 58, "z" * 58):
            self.s._update_settings({"device_name": device_name})
            name = self.s._sendspin_name()
            self.assertTrue(name.endswith(" audio"), name)
            self.assertLessEqual(len(name.encode()), 64, name)
            self.assertNotIn("  ", name)
        self.s._update_settings({"device_name": "Short"})
        self.assertEqual(self.s._sendspin_name(), "Short audio")

    def test_the_private_key_is_created_once_and_stays(self):
        first = self.s._sendspin_private_key()
        self.assertEqual(len(first), 32)
        self.assertEqual(Settings()._sendspin_private_key(), first)
        self.assertEqual(self.s._load_settings()["sendspin_private_key"], first.hex())

    def test_a_bad_stored_key_is_replaced(self):
        for bad in ("xyz", "ab" * 31, "AB" * 32, 5, None):
            self.s._update_settings({"sendspin_private_key": bad})
            key = self.s._sendspin_private_key()
            self.assertEqual(len(key), 32, msg=repr(bad))
            self.assertRegex(self.s._load_settings()["sendspin_private_key"], r"^[0-9a-f]{64}$")

    def test_the_key_is_never_returned_to_the_panel_but_the_other_keys_are(self):
        key = self.s._sendspin_private_key().hex()
        result = asyncio.run(self.s.get_settings())
        self.assertNotIn("sendspin_private_key", result)
        self.assertNotIn(key, json.dumps(result))
        self.assertEqual((result["sendspin_enabled"], result["sendspin_volume"]), (True, 50))

    def test_values_are_validated_on_load(self):
        bad = {**default_settings(), "sendspin_enabled": "yes", "sendspin_volume": 101, "sendspin_muted": 1, "sendspin_output_delay_ms": 6000}
        cleaned = clean_sendspin(bad)
        self.assertEqual((cleaned["sendspin_enabled"], cleaned["sendspin_volume"], cleaned["sendspin_muted"], cleaned["sendspin_output_delay_ms"]),
                         (True, 50, False, 0))
        for volume in (-1, True, 12.5, "5"):
            self.assertEqual(clean_sendspin({**default_settings(), "sendspin_volume": volume})["sendspin_volume"], 50)
        self.assertEqual(clean_sendspin({**default_settings(), "sendspin_volume": 0})["sendspin_volume"], 0)

    def test_saving_volume_and_mute_keeps_everything_else_and_writes_only_on_change(self):
        self.s._update_settings({"secret": "deadbeef"})
        key = self.s._sendspin_private_key()
        self.s._save_sendspin(sendspin_volume=20, sendspin_muted=True, sendspin_output_delay_ms=250)
        loaded = Settings()._load_settings()
        self.assertEqual((loaded["sendspin_volume"], loaded["sendspin_muted"], loaded["sendspin_output_delay_ms"]), (20, True, 250))
        self.assertEqual((loaded["secret"], bytes.fromhex(loaded["sendspin_private_key"])), ("deadbeef", key))
        mtime = os.stat(self.path).st_mtime_ns
        self.s._save_sendspin(sendspin_volume=20)
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)  # nothing changed, nothing written

    def test_an_invalid_value_is_not_saved_and_unknown_keys_are_refused(self):
        self.s._save_sendspin(sendspin_volume=30)
        for patch in ({"sendspin_volume": 500}, {"sendspin_muted": "yes"}, {"sendspin_output_delay_ms": -1}, {"sendspin_enabled": 1}):
            with self.assertRaises(ValueError, msg=patch):
                self.s._save_sendspin(**patch)
        self.assertEqual(self.s._load_settings()["sendspin_volume"], 30)
        with self.assertRaises(KeyError):
            self.s._save_sendspin(secret="x")
        with self.assertRaises(KeyError):
            self.s._save_sendspin(sendspin_private_key="00" * 32)

    def test_the_file_stays_mode_600_with_the_key_in_it(self):
        self.s._sendspin_private_key()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
