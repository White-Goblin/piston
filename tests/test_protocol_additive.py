"""Rule: protocol changes are additive. A command, state group, event, frame type, error code, close code or version that a paired peer may
rely on is never removed or renamed (tests/protocol_snapshot.json lists them). New names pass without any edit here.

If a change really cannot be additive: bump PROTOCOL_VERSION in piston_device/proto/__init__.py, edit the snapshot in the same commit, and
say in the changelog that the plugin and the integration must be updated together (docs/releasing.md, docs/protocol.md)."""
import ast
import json
import os
import unittest

from piston_device import server
from piston_device.proto import LANE_VERSION, PROTOCOL_VERSION, messages as m

from . import ROOT

HOW_TO = ("Protocol names are never removed or renamed: a paired peer on another version would break. Keep the old name working and add "
          "the new one. If a break is unavoidable, bump the protocol version and edit tests/protocol_snapshot.json in the same commit "
          "(see docs/releasing.md and docs/protocol.md).")


def device_trees():
    files = ["main.py"] + [os.path.join(folder, n) for folder, _d, names in os.walk(os.path.join(ROOT, "piston_device")) for n in names if n.endswith(".py")]
    for path in files:
        with open(path if os.path.isabs(path) else os.path.join(ROOT, path), encoding="utf-8") as f:
            yield ast.parse(f.read())


def first_string_args(call_names) -> set:
    found = set()
    for tree in device_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                if name in call_names:
                    found.add(node.args[0].value)
    return found


def command_names() -> set:
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    commands = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_commands")
    table = next(n for n in ast.walk(commands) if isinstance(n, ast.Dict))
    return {k.value for k in table.keys if isinstance(k, ast.Constant)} | {server.RESYNC}


def load_snapshot() -> dict:
    with open(os.path.join(ROOT, "tests", "protocol_snapshot.json"), encoding="utf-8") as f:
        return json.load(f)


def missing(required, actual) -> list:
    return sorted(set(required) - set(actual))


class AdditiveProtocolTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = load_snapshot()

    def test_no_command_has_been_removed_or_renamed(self):
        self.assertEqual(missing(self.snapshot["commands"], command_names()), [], HOW_TO)

    def test_no_state_group_has_been_removed_or_renamed(self):
        groups = first_string_args({"_push"}) | {m.MEDIA_GROUP, m.SENDSPIN_GROUP}
        self.assertEqual(missing(self.snapshot["state_groups"], groups), [], HOW_TO)

    def test_no_event_has_been_removed_or_renamed(self):
        self.assertEqual(missing(self.snapshot["events"], first_string_args({"_push_event", "push_event"})), [], HOW_TO)

    def test_no_frame_type_or_error_code_has_been_removed_or_renamed(self):
        self.assertEqual(missing(self.snapshot["frame_types"], m.FRAME_TYPES), [], HOW_TO)
        self.assertEqual(missing(self.snapshot["error_codes"], m.ERROR_CODES), [], HOW_TO)

    def test_the_close_codes_keep_their_values(self):
        actual = {name: getattr(server, name, None) for name in self.snapshot["close_codes"]}
        self.assertEqual(actual, self.snapshot["close_codes"], HOW_TO)

    def test_the_versions_never_go_down(self):
        self.assertGreaterEqual(PROTOCOL_VERSION, self.snapshot["protocol_version"], HOW_TO)
        self.assertGreaterEqual(LANE_VERSION, self.snapshot["lane_version"], HOW_TO)

    def test_additions_pass_and_removals_are_reported(self):
        self.assertEqual(missing(["a", "b"], ["a", "b", "c"]), [])
        self.assertEqual(missing(["a", "b"], ["a", "c"]), ["b"])
