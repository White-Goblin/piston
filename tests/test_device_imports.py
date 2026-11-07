"""Rule: device code imports only the standard library, Decky and itself (Decky has no runtime pip; system libraries go through ctypes)."""
import ast
import os
import sys
import unittest

from . import ROOT

ALLOWED = {"decky", "piston_device"}
EXCEPTIONS = {}  # path -> {module: reason}; empty on purpose: add an entry only with a reason


def third_party_imports(source: str) -> set:
    """Top-level names imported (absolutely) by `source` that are neither standard library nor allowed. Imports inside an
    `if TYPE_CHECKING:` block are not executed on the device and are skipped."""
    tree = ast.parse(source)
    skipped = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and (getattr(node.test, "id", None) == "TYPE_CHECKING" or getattr(node.test, "attr", None) == "TYPE_CHECKING"):
            skipped.update(id(n) for stmt in node.body for n in ast.walk(stmt))
    found = set()
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return {name for name in found if name not in sys.stdlib_module_names and name not in ALLOWED}


def device_files():
    yield "main.py"
    for folder, _dirs, names in os.walk(os.path.join(ROOT, "piston_device")):
        for name in names:
            if name.endswith(".py"):
                yield os.path.relpath(os.path.join(folder, name), ROOT)


class DeviceImportTests(unittest.TestCase):
    def test_the_device_code_imports_only_the_standard_library_decky_and_itself(self):
        problems = []
        for rel in device_files():
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                extra = third_party_imports(f.read()) - set(EXCEPTIONS.get(rel, {}))
            if extra:
                problems.append(f"{rel} imports {sorted(extra)}")
        self.assertEqual(problems, [], "Device code must not depend on third-party packages: Decky has no runtime pip. "
                                       "Use the standard library, or ctypes over a system library (see docs/architecture.md).")

    def test_a_third_party_import_is_found(self):
        self.assertEqual(third_party_imports("import os\nimport requests\nfrom aiohttp.web import x\nfrom . import y\nimport decky\n"),
                         {"requests", "aiohttp"})

    def test_imports_under_type_checking_are_ignored(self):
        self.assertEqual(third_party_imports("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import numpy\n"), set())
