import json
import os
import sys
import tempfile
import unittest
import zipfile

from . import ROOT

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import package_plugin  # noqa: E402

INTEGRATION = os.path.join(ROOT, "custom_components", "piston")


def readme_and_reference():
    return read("README.md") + read("docs", "reference.md")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class DocsMatchTheCode(unittest.TestCase):
    def test_readme_lists_every_entity_and_action(self):
        readme = readme_and_reference()
        strings = json.loads(read("custom_components", "piston", "translations", "en.json"))
        for platform, entities in strings["entity"].items():
            for entity in entities.values():
                self.assertIn(entity["name"], readme, f"{platform} entity {entity['name']!r} is not in the README")
        for service in strings["services"]:
            self.assertIn(f"piston.{service}", readme, f"action {service} is not in the README")

    def test_readme_lists_every_option(self):
        readme = readme_and_reference()
        for label in json.loads(read("custom_components", "piston", "translations", "en.json"))["options"]["step"]["init"]["data"].values():
            self.assertIn(label, readme, f"option {label!r} is not in the README")

    def test_the_remote_card_docs_name_its_commands_statuses_events_and_config_keys(self):
        from custom_components.piston import input_api, input_status
        from piston_device.proto import messages
        reference, protocol = read("docs", "reference.md"), read("docs", "protocol.md")
        for name in (input_api.SUBSCRIBE, input_api.SEND):
            self.assertIn(name, protocol)
        for status in input_status.STATUSES:
            self.assertIn(f"`{status}`", protocol, f"status {status} is not in protocol.md")
        for key in ("device_id", "speed", "show_power", "show_apps", "show_volume", "show_keyboard", "show_transport"):
            self.assertIn(f"`{key}`", reference, f"card key {key} is not in the reference")
        for key in messages.INPUT_KEYS:
            self.assertIn(key, protocol)
        for kind in ("move", "scroll", "click", "key", "text", "action", "seek", "release"):
            self.assertIn(f'"k":"{kind}"', protocol.replace(" ", ""), f"event {kind} is not in protocol.md")
        self.assertIn("piston-remote-card", read("README.md"))

    def test_readme_names_the_port_and_service_type(self):
        from custom_components.piston.const import DEFAULT_PORT, SERVICE_TYPE
        text = readme_and_reference() + read("docs", "protocol.md")
        self.assertIn(str(DEFAULT_PORT), text)
        self.assertIn(SERVICE_TYPE.rstrip("."), text)

    def test_the_protocol_doc_describes_the_current_version_and_every_command_and_event(self):
        import main
        from piston_device.proto import MIN_PROTOCOL_VERSION, PROTOCOL_VERSION
        doc = read("docs", "protocol.md")
        self.assertIn(f"protocol version {PROTOCOL_VERSION}", doc)
        self.assertEqual(MIN_PROTOCOL_VERSION, PROTOCOL_VERSION)          # the doc says both sides accept only this version
        for command in list(main.Plugin()._commands()) + ["resync"]:
            self.assertIn(f"`{command}", doc, f"command {command} is not in docs/protocol.md")
        for event in ("going_to_sleep", "shutting_down", "resumed", "toast_click", "pip_signal"):
            self.assertIn(f"`{event}", doc, f"event {event} is not in docs/protocol.md")
        for term in ("boot_id", "seq", "coalesced", "version_skew", "app_sync", "ping"):
            self.assertIn(term, doc, term)

    def test_the_readme_explains_what_the_media_player_shows(self):
        readme = readme_and_reference()
        for term in ("standby", "off", "unavailable", "Turn on", "Repairs", "Download diagnostics", "Reconfigure"):
            self.assertIn(term, readme, term)

    def test_the_changelog_has_a_section_for_this_version_that_says_to_update_both_sides_together(self):
        changelog = read("CHANGELOG.md")
        version = json.loads(read("package.json"))["version"]
        self.assertIn(f"## {version}", changelog)
        section = changelog.split(f"## {version}", 1)[1].split("\n## ", 1)[0]
        self.assertIn("together", section)
        from piston_device.proto import PROTOCOL_VERSION
        self.assertIn(f"protocol version {PROTOCOL_VERSION}", section)

    def test_the_protocol_doc_describes_the_update_lane_and_the_first_lane_version_is_not_in_the_future(self):
        from custom_components.piston.const import FIRST_LANE_VERSION
        from custom_components.piston.update_logic import version_key
        from piston_device.proto import messages as m
        doc = read("docs", "protocol.md")
        for term in ("plugin_version", "lane", "`update ", "`update_status ", "SHA256SUMS", "waiting_for_confirmation", "not_confirmed",
                     "approves it", "frozen") + tuple(m.UPDATE_STATES) + tuple(m.UPDATE_REASONS):
            self.assertIn(term, doc, term)
        manifest = json.loads(read("custom_components", "piston", "manifest.json"))
        self.assertLessEqual(version_key(FIRST_LANE_VERSION), version_key(manifest["version"]))

    def test_the_readme_explains_the_plugin_update_and_the_approval(self):
        readme = readme_and_reference()
        for term in ("Plugin update", "Install plugin updates automatically", "approve", "game mode", "not approved", "SHA256SUMS"):
            self.assertIn(term, readme, term)

    def test_markdown_links_to_local_files_resolve(self):
        import re
        for doc in ("README.md", "docs/reference.md", "docs/protocol.md", "docs/development.md", "docs/releasing.md", "CHANGELOG.md"):
            base = os.path.dirname(os.path.join(ROOT, doc))
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", read(*doc.split("/"))):
                if "://" in target or target.startswith("mailto:"):
                    continue
                self.assertTrue(os.path.exists(os.path.normpath(os.path.join(base, target))), f"{doc}: broken link {target}")

    def test_versions_agree(self):
        manifest = json.loads(read("custom_components", "piston", "manifest.json"))
        self.assertEqual(manifest["version"], json.loads(read("package.json"))["version"])


class PackagingTests(unittest.TestCase):
    def make_tree(self, root):
        for path, text in {"main.py": "x", "plugin.json": "{}", "LICENSE": "l", "package.json": json.dumps({"version": "9.9.9"}),
                           "dist/index.js": "j", "dist/index.js.map": "m", "dist/player.js": "p", "piston_device/a.py": "a",
                           "piston_device/__pycache__/a.cpython.pyc": "c", "py_modules/.keep": ""}.items():
            full = os.path.join(root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)

    def test_zip_has_one_plugin_folder_without_maps_or_caches(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            self.make_tree(root)
            target = package_plugin.build(out, root)
            names = sorted(zipfile.ZipFile(target).namelist())
        self.assertTrue(target.endswith("piston-9.9.9.zip"))
        self.assertEqual(names, sorted(f"Piston/{n}" for n in (
            "LICENSE", "piston_device/a.py", "dist/index.js", "dist/player.js", "main.py", "package.json", "plugin.json", "py_modules/.keep")))

    def test_checksums_file_matches_the_zip_in_sha256sum_format(self):
        import hashlib
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            self.make_tree(root)
            target = package_plugin.build(out, root)
            with open(os.path.join(out, "SHA256SUMS"), encoding="utf-8") as f:
                lines = f.read().splitlines()
            with open(target, "rb") as z:
                digest = hashlib.sha256(z.read()).hexdigest()
        self.assertEqual(lines, [f"{digest}  piston-9.9.9.zip"])

    def test_release_workflow_attaches_the_checksums(self):
        self.assertIn("out/SHA256SUMS", read(".github", "workflows", "release.yml"))

    def test_refuses_to_package_without_a_build(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(SystemExit):
                package_plugin.collect(root)


class HacsRequirements(unittest.TestCase):
    """What the HACS action checks for an integration repository, verified here because the action itself only
    runs on public repositories."""

    def test_repository_layout_and_manifest(self):
        folders = [d for d in os.listdir(os.path.join(ROOT, "custom_components")) if not d.startswith("__")]
        self.assertEqual(folders, ["piston"])
        manifest = json.loads(read("custom_components", "piston", "manifest.json"))
        for key in ("domain", "name", "codeowners", "documentation", "issue_tracker", "version"):
            self.assertTrue(manifest.get(key), f"manifest.json needs {key}")
        self.assertEqual(manifest["domain"], folders[0])
        hacs = json.loads(read("hacs.json"))
        self.assertTrue(hacs.get("name"))
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "README.md")))

    def test_brand_icon_is_a_png(self):
        with open(os.path.join(INTEGRATION, "brand", "icon.png"), "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_translations_cover_every_config_flow_step(self):
        strings = json.loads(read("custom_components", "piston", "strings.json"))
        translated = json.loads(read("custom_components", "piston", "translations", "en.json"))
        self.assertEqual(strings, translated)
        for step in ("user", "confirm", "pair", "reauth_confirm"):
            self.assertIn(step, translated["config"]["step"])
