import os
import re
import unittest

from . import ROOT

SRC = os.path.join(ROOT, "piston_device", "proto")
DST = os.path.join(ROOT, "custom_components", "piston", "proto")


def _files(d):
    return sorted(n for n in os.listdir(d) if n.endswith(".py"))


class ProtoSyncTests(unittest.TestCase):
    def test_copies_identical(self):
        self.assertEqual(_files(SRC), _files(DST), "run scripts/sync_proto.py")
        for name in _files(SRC):
            with open(os.path.join(SRC, name), "rb") as a, open(os.path.join(DST, name), "rb") as b:
                self.assertEqual(a.read(), b.read(), f"{name} differs: run scripts/sync_proto.py")

    def test_no_forbidden_imports(self):
        for name in _files(SRC):
            with open(os.path.join(SRC, name)) as f:
                text = f.read()
            self.assertIsNone(re.search(r"^\s*(import|from)\s+(decky|homeassistant)", text, re.M), name)
