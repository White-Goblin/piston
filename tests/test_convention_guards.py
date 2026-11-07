"""Guards for conventions that used to be unenforced: who may touch the audio output, no self-killing process patterns, tests that run
Plugin._main() use the environment patches, and the module map in docs/modules.md lists every source file.

When one fails: the message names the file. For the module map, add a line for the file to docs/modules.md."""
import ast
import glob
import os
import re
import subprocess
import unittest

from . import ROOT

SINK_CALLS = {"open", "write", "write_silence", "flush", "set_volume", "set_mute", "close", "abort"}
AUDIO_OWNER = "piston_device/sendspin/session.py"
SINK_FILE = "piston_device/sendspin/sink.py"


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def tracked(*patterns: str) -> list:
    out = subprocess.check_output(["git", "ls-files", "-z", "-c", "-o", "--exclude-standard"], cwd=ROOT).decode().split("\0")
    return sorted(f for f in out if f and os.path.isfile(os.path.join(ROOT, f)) and any(glob.fnmatch.fnmatch(f, p) for p in patterns))


def sink_calls(source: str) -> list:
    """(line, method) for each call written as `<x>.sink.<method>(...)` or `sink.<method>(...)` with a method that drives the output."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in SINK_CALLS:
            owner = node.func.value
            name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
            if name in ("sink", "_sink"):
                found.append((node.lineno, node.func.attr))
    return found


def self_kill_patterns(text: str) -> list:
    """pgrep/pkill with -f and a pattern that is not protected by a [x]yz bracket."""
    bad = []
    for m in re.finditer(r"\b(?:pgrep|pkill)\s+((?:-\w+\s+)*)(\S+)", text):
        flags, pattern = m.group(1), m.group(2)
        if re.search(r"-\w*f", flags) and not re.search(r"\[[^\]]+\]", pattern):
            bad.append(m.group(0))
    return bad


class AudioOwnerTests(unittest.TestCase):
    def test_only_the_session_drives_the_audio_output(self):
        offenders = []
        for rel in tracked("piston_device/*.py", "piston_device/*/*.py", "main.py"):
            if rel in (AUDIO_OWNER, SINK_FILE):
                continue
            offenders += [f"{rel}:{line} sink.{method}()" for line, method in sink_calls(read(rel))]
        self.assertEqual(offenders, [], f"Only the audio task in {AUDIO_OWNER} may open, write to, adjust or close the output.")

    def test_the_owner_is_still_where_the_guard_looks(self):
        self.assertTrue(sink_calls(read(AUDIO_OWNER)), "the guard no longer finds sink calls in the session: update AUDIO_OWNER")

    def test_detection(self):
        self.assertEqual(sink_calls("async def f(self):\n await self.sink.write(b'')\n x.other.write(1)"), [(2, "write")])


class NoSelfKillTests(unittest.TestCase):
    def test_no_tracked_script_uses_an_unbracketed_pattern_with_f(self):
        offenders = []
        for rel in tracked("*.py", "*.sh", "*.yml", "*.yaml", "*.ts", "*.tsx", "*.js", "*.json"):
            if rel == "tests/test_convention_guards.py" or rel.startswith(("node_modules/", ".venv")):
                continue
            offenders += [f"{rel}: {hit}" for hit in self_kill_patterns(read(rel))]
        self.assertEqual(offenders, [], "pgrep/pkill -f with a pattern that appears in your own command line kills your shell: use [x]yz or a PID.")

    def test_detection(self):
        self.assertEqual(self_kill_patterns("pkill -f plugin_loader"), ["pkill -f plugin_loader"])
        self.assertEqual(self_kill_patterns("pkill -f '[p]lugin_loader'"), [])
        self.assertEqual(self_kill_patterns("pgrep gdbus"), [])


class PluginEnvironmentTests(unittest.TestCase):
    def test_tests_that_run_plugin_main_use_the_environment_patches(self):
        offenders = []
        for rel in tracked("tests/test_*.py"):
            if rel == "tests/test_convention_guards.py":
                continue
            source = read(rel)
            runs_main = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_main"
                            for n in ast.walk(ast.parse(source)))
            if runs_main and "plugin_environment_patches" not in source:
                offenders.append(rel)
        self.assertEqual(offenders, [], "These tests call Plugin._main() without tests.plugin_environment_patches(); they would start gdbus on the host.")


class ModuleMapTests(unittest.TestCase):
    def test_every_source_file_is_in_the_module_map(self):
        page = read("docs/modules.md")
        missing = []
        for rel in tracked("main.py", "piston_device/*.py", "piston_device/sendspin/*.py", "custom_components/piston/*.py",
                           "custom_components/piston/www/*.js", "scripts/*.py", "src/*.ts", "src/*.tsx", "src/*/*.ts", "src/*/*.tsx", "src/*/*.html"):
            name = os.path.basename(rel)
            if rel.startswith("src/") and rel.count("/") == 2:  # the frontend is documented per folder (or per file when named)
                folder = rel.split("/")[1]
                if f"`{folder}/`" not in page and f"`{rel[4:]}`" not in page and f"`{name}`" not in page:
                    missing.append(rel)
                continue
            if name == "__init__.py" and not rel.endswith(("piston_device/__init__.py", "piston/__init__.py")):
                continue  # package markers; the package itself is named in the page
            if f"`{name}`" not in page and f"`{rel}`" not in page and not re.search(rf"`[^`]*\b{re.escape(name)}\b[^`]*`", page):
                missing.append(rel)
        self.assertEqual(missing, [], "Add a one-line entry for each file to docs/modules.md.")
