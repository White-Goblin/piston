"""Rule: every long-lived device component has a synchronous abort() that Plugin._unload_now reaches.

Decky stops serving the plugin's event loop right after it asks for the unload, so everything that must happen on unload runs
in `_unload_now` before its first await (docs/architecture.md). To add a component: give it a plain `def abort(self)`, call it
from `_unload_now` (directly or through a Plugin-level abort such as `_input_abort`), and add it to COMPONENTS below."""
import ast
import os
import unittest

from . import ROOT

# (file, class) of every class that holds a socket, task, subprocess or device and is torn down on unload
COMPONENTS = {
    ("piston_device/lifecycle.py", "Lifecycle"),
    ("piston_device/server.py", "ProtocolServer"),
    ("piston_device/advertise.py", "Advertiser"),
    ("piston_device/mdns.py", "MdnsAdvertiser"),
    ("piston_device/cursor.py", "CursorFeed"),
    ("piston_device/sendspin/listener.py", "SendspinListener"),
    ("piston_device/sendspin/session.py", "SendspinSession"),
    ("piston_device/sendspin/sink.py", "Sink"),
    ("piston_device/sendspin/sink.py", "FakeSink"),
    ("piston_device/sendspin/sink.py", "PipeWireSink"),
}
# Plugin-level synchronous entry points that _unload_now must call (name as written in main.py), and who they reach
PLUGIN_ENTRY_POINTS = ("_sendspin_abort", "_abort_lifecycle", "_input_abort")
HOW_TO = "Give the component a plain `def abort(self)`, call it from Plugin._unload_now and add it to COMPONENTS in tests/test_abort_everywhere.py."


def parse(rel: str) -> ast.Module:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return ast.parse(f.read())


def abort_methods(tree: ast.AST) -> dict:
    """class name -> the ast node of its `abort` (FunctionDef or AsyncFunctionDef), for classes that define one."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "abort":
                    found[node.name] = item
    return found


def function(tree: ast.AST, name: str):
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def awaits(node: ast.AST) -> list:
    return [n for n in ast.walk(node) if isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith))]


class AbortEverywhereTests(unittest.TestCase):
    def test_every_listed_component_has_a_plain_abort(self):
        problems = []
        for rel, cls in sorted(COMPONENTS):
            methods = abort_methods(parse(rel))
            if cls not in methods:
                problems.append(f"{rel}: {cls} has no abort()")
            elif isinstance(methods[cls], ast.AsyncFunctionDef):
                problems.append(f"{rel}: {cls}.abort() is async; it must be a plain def")
        self.assertEqual(problems, [], HOW_TO)

    def test_no_class_with_an_abort_is_missing_from_the_list(self):
        listed = {(rel, cls) for rel, cls in COMPONENTS}
        unlisted = []
        for folder, _dirs, names in os.walk(os.path.join(ROOT, "piston_device")):
            for name in names:
                if name.endswith(".py"):
                    rel = os.path.relpath(os.path.join(folder, name), ROOT)
                    unlisted += [f"{rel}: {cls}" for cls in abort_methods(parse(rel)) if (rel, cls) not in listed]
        self.assertEqual(unlisted, [], HOW_TO)

    def test_unload_now_does_not_await_and_reaches_every_entry_point(self):
        main = parse("main.py")
        unload = function(main, "_unload_now")
        self.assertIsInstance(unload, ast.FunctionDef, "_unload_now must be a plain def")
        self.assertEqual(awaits(unload), [], "_unload_now must not await: Decky starves the event loop after the unload request")
        text = ast.unparse(unload)
        for name in PLUGIN_ENTRY_POINTS + ("advertiser", "server"):
            self.assertIn(name, text, f"_unload_now does not reach {name}")
        for name in PLUGIN_ENTRY_POINTS:
            node = next((n for rel in ("main.py", "piston_device/input.py", "piston_device/sendspin/mixin.py")
                         for n in ast.walk(parse(rel)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
            self.assertIsInstance(node, ast.FunctionDef, f"{name} must exist and be a plain def")
        self.assertIn("cursor_feed.abort", ast.unparse(function(parse("piston_device/input.py"), "_input_abort")))

    def test_the_checks_notice_an_async_abort_a_missing_abort_and_an_await(self):
        tree = ast.parse("class A:\n    async def abort(self):\n        pass\nclass B:\n    def run(self):\n        pass\n")
        self.assertIsInstance(abort_methods(tree)["A"], ast.AsyncFunctionDef)
        self.assertNotIn("B", abort_methods(tree))
        self.assertEqual(len(awaits(ast.parse("async def f():\n    await x\n"))), 1)
