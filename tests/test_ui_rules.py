"""Rule: never use `tabsHook.add`; no hard-coded screen sizes or device model names in code or user-facing text.

`tabsHook.add` makes Decky push its tabs again on every render and corrupts the Steam menu (docs/architecture.md); the media tab
wraps `tabsHook.render` instead. The plugin runs on several devices, so the code asks the screen for its size and the text says
"device". Prose in README.md and docs/ may name supported hardware: only code and strings are checked here."""
import os
import re
import unittest

from . import ROOT

CODE_ROOTS = ("src", "piston_device", os.path.join("custom_components", "piston"))
CODE_FILES = ("main.py",)
SUFFIXES = (".py", ".ts", ".tsx", ".js", ".json", ".yaml", ".html")
SKIP_PARTS = ("node_modules", "__pycache__", os.sep + "brand" + os.sep)

TABS_ADD = re.compile(r"tabsHook\s*\.\s*add\s*\(")
SCREEN_SIZE = re.compile(r"\b(1280|1366|1920|2560|3840)\b|\b(720|800|1080|1440|2160)\b")
MODEL_NAME = re.compile(r"steam\s*deck|steam\s*machine|steamdeck|rog\s*ally|legion\s*go", re.IGNORECASE)

# Files that may contain a screen size, each a default used only until the real size is known (path -> reason)
SCREEN_SIZE_OK = {
    "piston_device/pointer.py": "default size until the frontend reports the real screen",
    "src/cursor/CursorOverlay.tsx": "fallback when the window reports no size",
    "src/pip/PipOverlay.tsx": "fallback when the window reports no size",
}
# Files that may name a hardware model, each because the hardware itself uses the name (path -> reason)
MODEL_NAME_OK = {
    "piston_device/sensors.py": "the kernel's hwmon device name for the fan sensor",
}


def code_files():
    for rel in CODE_FILES:
        yield rel
    for root in CODE_ROOTS:
        for folder, _dirs, names in os.walk(os.path.join(ROOT, root)):
            if any(part in folder + os.sep for part in SKIP_PARTS):
                continue
            for name in names:
                if name.endswith(SUFFIXES) and not name.endswith(".min.js"):
                    yield os.path.relpath(os.path.join(folder, name), ROOT).replace(os.sep, "/")


def hits(pattern, source: str) -> list:
    return [n for n, line in enumerate(source.split("\n"), 1) if pattern.search(line)]


def scan(pattern, allowed=()) -> list:
    found = []
    for rel in code_files():
        if rel in allowed:
            continue
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            found += [f"{rel}:{n}" for n in hits(pattern, f.read())]
    return found


class UiRuleTests(unittest.TestCase):
    def test_tabs_hook_add_is_never_called(self):
        self.assertEqual(scan(TABS_ADD), [], "Never call tabsHook.add: it corrupts the Steam menu. Wrap tabsHook.render as src/media/registerTab.tsx does.")

    def test_no_hard_coded_screen_size(self):
        self.assertEqual(scan(SCREEN_SIZE, SCREEN_SIZE_OK), [],
                         "Do not hard-code a screen size: read it from the screen. A default that applies only until the real size is "
                         "known goes in SCREEN_SIZE_OK in tests/test_ui_rules.py, with the reason.")

    def test_no_device_model_name_in_code_or_strings(self):
        self.assertEqual(scan(MODEL_NAME, MODEL_NAME_OK), [], "Say \"device\", not a model name, in code and user-facing strings.")

    def test_the_allowlists_name_files_that_still_need_them(self):
        for allowed, pattern in ((SCREEN_SIZE_OK, SCREEN_SIZE), (MODEL_NAME_OK, MODEL_NAME)):
            for rel in allowed:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    self.assertTrue(hits(pattern, f.read()), f"{rel} no longer needs its allowlist entry: remove it")

    def test_the_patterns_catch_what_they_should(self):
        self.assertEqual(hits(TABS_ADD, "a()\nDeckyPluginLoader.tabsHook.add( tab )\n"), [2])
        self.assertEqual(hits(TABS_ADD, "`tabsHook.add` must never be used"), [])
        self.assertEqual(hits(SCREEN_SIZE, "w = 1280\nh = 800\nms = 8000\nx = 1.5"), [1, 2])
        self.assertEqual(hits(MODEL_NAME, "Steam Deck\nsteamdeck_hwmon\ndevice"), [1, 2])
