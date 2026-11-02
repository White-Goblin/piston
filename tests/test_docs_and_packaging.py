import json
import os
import sys
import tempfile
import unittest
import zipfile

from . import ROOT

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import package_plugin  # noqa: E402

INTEGRATION = os.path.join(ROOT, "custom_components", "decky_home_assistant")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class DocsMatchTheCode(unittest.TestCase):
    def test_readme_lists_every_entity_and_action(self):
        readme = read("README.md")
        strings = json.loads(read("custom_components", "decky_home_assistant", "translations", "en.json"))
        for platform, entities in strings["entity"].items():
            for entity in entities.values():
                self.assertIn(entity["name"], readme, f"{platform} entity {entity['name']!r} is not in the README")
        for service in strings["services"]:
            self.assertIn(f"decky_home_assistant.{service}", readme, f"action {service} is not in the README")

    def test_readme_lists_every_option(self):
        readme = read("README.md")
        for label in json.loads(read("custom_components", "decky_home_assistant", "translations", "en.json"))["options"]["step"]["init"]["data"].values():
            self.assertIn(label, readme, f"option {label!r} is not in the README")

    def test_readme_names_the_port_and_service_type(self):
        from custom_components.decky_home_assistant.const import DEFAULT_PORT, SERVICE_TYPE
        text = read("README.md") + read("docs", "protocol.md")
        self.assertIn(str(DEFAULT_PORT), text)
        self.assertIn(SERVICE_TYPE.rstrip("."), text)

    def test_markdown_links_to_local_files_resolve(self):
        import re
        for doc in ("README.md", "docs/protocol.md", "docs/development.md", "docs/releasing.md"):
            base = os.path.dirname(os.path.join(ROOT, doc))
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", read(*doc.split("/"))):
                if "://" in target or target.startswith("mailto:"):
                    continue
                self.assertTrue(os.path.exists(os.path.normpath(os.path.join(base, target))), f"{doc}: broken link {target}")

    def test_versions_agree(self):
        manifest = json.loads(read("custom_components", "decky_home_assistant", "manifest.json"))
        self.assertEqual(manifest["version"], json.loads(read("package.json"))["version"])


class PackagingTests(unittest.TestCase):
    def make_tree(self, root):
        for path, text in {"main.py": "x", "plugin.json": "{}", "LICENSE": "l", "package.json": json.dumps({"version": "9.9.9"}),
                           "dist/index.js": "j", "dist/index.js.map": "m", "dist/player.js": "p", "decky_ha/a.py": "a",
                           "decky_ha/__pycache__/a.cpython.pyc": "c", "py_modules/.keep": ""}.items():
            full = os.path.join(root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)

    def test_zip_has_one_plugin_folder_without_maps_or_caches(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            self.make_tree(root)
            target = package_plugin.build(out, root)
            names = sorted(zipfile.ZipFile(target).namelist())
        self.assertTrue(target.endswith("decky-home-assistant-9.9.9.zip"))
        self.assertEqual(names, sorted(f"Decky Home Assistant/{n}" for n in (
            "LICENSE", "decky_ha/a.py", "dist/index.js", "dist/player.js", "main.py", "package.json", "plugin.json", "py_modules/.keep")))

    def test_refuses_to_package_without_a_build(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(SystemExit):
                package_plugin.collect(root)


class HacsRequirements(unittest.TestCase):
    """What the HACS action checks for an integration repository, verified here because the action itself only
    runs on public repositories."""

    def test_repository_layout_and_manifest(self):
        folders = [d for d in os.listdir(os.path.join(ROOT, "custom_components")) if not d.startswith("__")]
        self.assertEqual(folders, ["decky_home_assistant"])
        manifest = json.loads(read("custom_components", "decky_home_assistant", "manifest.json"))
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
        strings = json.loads(read("custom_components", "decky_home_assistant", "strings.json"))
        translated = json.loads(read("custom_components", "decky_home_assistant", "translations", "en.json"))
        self.assertEqual(strings, translated)
        for step in ("user", "confirm", "pair", "reauth_confirm"):
            self.assertIn(step, translated["config"]["step"])
