import hashlib
import unittest

from custom_components.piston import update_logic as ul
from custom_components.piston.const import FIRST_LANE_VERSION, RELEASE_REPO
from piston_device import update_check

H = "a" * 64


class DecideTests(unittest.TestCase):
    def decide(self, plugin="0.4.0", target="0.4.1", auto=True, pressed=False, latch=None):
        return ul.decide(plugin, target, auto=auto, pressed=pressed, latch=latch)

    def test_no_lane_or_garbage_version(self):
        for plugin in (None, "", "dev", "1.2"):
            self.assertEqual(self.decide(plugin=plugin), ul.NO_LANE)

    def test_up_to_date(self):
        self.assertEqual(self.decide(plugin="0.4.1"), ul.UP_TO_DATE)

    def test_send_with_auto_available_without(self):
        self.assertEqual(self.decide(), ul.SEND)
        self.assertEqual(self.decide(auto=False), ul.AVAILABLE)
        self.assertEqual(self.decide(auto=False, pressed=True), ul.SEND)

    def test_a_downgrade_is_sent_too(self):
        self.assertEqual(self.decide(plugin="0.5.0", target="0.4.1"), ul.SEND)

    def test_the_floor_blocks_targets_before_the_first_lane_release(self):
        self.assertEqual(self.decide(plugin=FIRST_LANE_VERSION, target="0.3.0", pressed=True), ul.PREDATES_LANE)
        self.assertEqual(self.decide(plugin="0.9.0", target="0.0.1"), ul.PREDATES_LANE)

    def test_the_latch_blocks_only_that_target_and_not_a_press(self):
        latch = {"version": "0.4.1", "reason": "hash"}
        self.assertEqual(self.decide(latch=latch), ul.LATCHED)
        self.assertEqual(self.decide(latch=latch, auto=False), ul.LATCHED)
        self.assertEqual(self.decide(latch=latch, pressed=True), ul.SEND)
        self.assertEqual(self.decide(target="0.4.2", latch=latch), ul.SEND)

    def test_versions_compare_numerically(self):
        self.assertEqual(self.decide(plugin="0.9.0", target="0.10.0"), ul.SEND)
        self.assertLess(ul.version_key("0.9.0"), ul.version_key("0.10.0"))


class SumsTests(unittest.IsolatedAsyncioTestCase):
    SUMS = f"{H}  piston-0.4.1.zip\n{'b' * 64}  other.txt\n"

    def test_parse_handles_sha256sum_output(self):
        text = f"{H}  a.zip\n{'B' * 64} *b.zip\n\nnot a line\n{'c' * 63}  short.zip\n"
        self.assertEqual(ul.parse_sha256sums(text), {"a.zip": H, "b.zip": "b" * 64})

    async def test_checksum_found_and_cached(self):
        calls = []

        async def get(url):
            calls.append(url)
            return self.SUMS

        cache = {}
        self.assertEqual(await ul.zip_checksum(get, "0.4.1", cache), H)
        self.assertEqual(await ul.zip_checksum(get, "0.4.1", cache), H)
        self.assertEqual(calls, [f"https://github.com/{RELEASE_REPO}/releases/download/v0.4.1/SHA256SUMS"])

    async def test_missing_entry_or_failed_fetch_is_none_and_not_cached(self):
        async def other(url):
            return f"{H}  something-else.zip\n"

        async def boom(url):
            raise TimeoutError

        cache = {}
        self.assertIsNone(await ul.zip_checksum(other, "0.4.1", cache))
        self.assertIsNone(await ul.zip_checksum(boom, "0.4.1", cache))
        self.assertEqual(cache, {})

    def test_urls_agree_with_the_device_side(self):
        self.assertEqual(ul.RELEASE_REPO if hasattr(ul, "RELEASE_REPO") else RELEASE_REPO, update_check.RELEASE_REPO)
        self.assertEqual(ul.asset_url("0.4.1"), update_check.asset_url("0.4.1"))
        self.assertTrue(ul.release_url("0.4.1").endswith("/releases/tag/v0.4.1"))

    def test_the_package_script_output_parses(self):
        import os, sys, tempfile, json
        from . import ROOT
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import package_plugin
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            for path, text in {"main.py": "x", "plugin.json": "{}", "LICENSE": "l", "package.json": json.dumps({"version": "9.9.9"}),
                               "dist/index.js": "j"}.items():
                full = os.path.join(root, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as f:
                    f.write(text)
            target = package_plugin.build(out, root)
            with open(os.path.join(out, "SHA256SUMS")) as f:
                sums = ul.parse_sha256sums(f.read())
            with open(target, "rb") as z:
                self.assertEqual(sums[ul.zip_name("9.9.9")], hashlib.sha256(z.read()).hexdigest())
