import io
import json
import os
import tempfile
import unittest
import zipfile

from piston_device import update_check as uc

from . import ROOT

SHA = "a" * 64


def make_zip(entries=None, version="1.2.3", extra=()):
    files = {"Piston/main.py": "x", "Piston/plugin.json": "{}",
             "Piston/package.json": json.dumps({"version": version})}
    files.update(entries or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
        for info, text in extra:
            z.writestr(info, text)
    return buf.getvalue()


class RequestTests(unittest.TestCase):
    def ok(self, **kw):
        args = dict(version="1.2.3", url=uc.asset_url("1.2.3"), sha256=SHA, installed="1.0.0")
        args.update(kw)
        uc.validate_request(**args)

    def refused(self, **kw):
        with self.assertRaises(uc.UpdateRefused) as cm:
            self.ok(**kw)
        self.assertEqual(cm.exception.reason, "bad_request")

    def test_a_good_request_passes(self):
        self.ok()
        self.ok(installed=None)

    def test_bad_versions(self):
        for v in ("1.2", "1.2.3-beta", "", None, 3, "1.2.3\n"):
            self.refused(version=v, url=uc.asset_url("1.2.3"))

    def test_bad_checksums(self):
        for s in ("A" * 64, "a" * 63, "", None, "g" * 64):
            self.refused(sha256=s)

    def test_urls_must_be_the_release_asset_of_that_version(self):
        base = uc.asset_url("1.2.3")
        for url in (base.replace("github.com", "evil.example"), base.replace("https", "http"),
                    base.replace("White-Goblin", "someone-else"), base.replace("v1.2.3", "v1.2.4"),
                    base + "?x=1", base.replace("piston-1.2.3.zip", "other.zip"),
                    base.replace("github.com", "github.com" + chr(64) + "evil.example"), "", None):
            self.refused(url=url)

    def test_the_installed_version_is_a_no_op(self):
        self.refused(installed="1.2.3")

    def test_version_key_orders_numerically(self):
        self.assertLess(uc.version_key("0.9.0"), uc.version_key("0.10.0"))


class ArchiveTests(unittest.TestCase):
    def refused(self, data, version="1.2.3"):
        with self.assertRaises(uc.UpdateRefused) as cm:
            uc.validate_archive(data, version)
        self.assertEqual(cm.exception.reason, "archive")
        return cm.exception.message

    def test_a_good_archive_passes(self):
        uc.validate_archive(make_zip(), "1.2.3")

    def test_not_a_zip(self):
        self.refused(b"nope")

    def test_path_escapes_and_foreign_folders(self):
        for name in ("../evil.py", "Piston/../../evil.py", "/etc/passwd", "Other/main.py", "main.py",
                     "Piston\\x.py", "C:/x.py"):
            self.refused(make_zip({name: "x"}))

    def test_symlinks(self):
        info = zipfile.ZipInfo("Piston/link")
        info.external_attr = (0o120777 << 16)
        self.assertIn("symbolic", self.refused(make_zip(extra=[(info, "/etc/passwd")])))

    def test_missing_required_files(self):
        for missing in ("main.py", "plugin.json", "package.json"):
            data = make_zip()
            buf = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as dst:
                for info in src.infolist():
                    if not info.filename.endswith(missing):
                        dst.writestr(info, src.read(info))
            self.assertIn(missing, self.refused(buf.getvalue()))

    def test_wrong_version_inside(self):
        self.assertIn("different version", self.refused(make_zip(version="1.2.4")))

    def test_unreadable_package_json(self):
        self.refused(make_zip({"Piston/package.json": "not json"}))

    def test_too_large_when_unpacked(self):
        from unittest import mock
        with mock.patch.object(uc, "MAX_UNPACKED_BYTES", 10):
            self.assertIn("too large", self.refused(make_zip({"Piston/big.bin": "x" * 100})))

    def test_a_zip_built_by_the_packager_passes(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import package_plugin
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            for path, text in {"main.py": "x", "plugin.json": "{}", "LICENSE": "l", "package.json": json.dumps({"version": "9.9.9"}),
                               "dist/index.js": "j", "piston_device/a.py": "a"}.items():
                full = os.path.join(root, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as f:
                    f.write(text)
            target = package_plugin.build(out, root)
            uc.validate_archive(target, "9.9.9")
