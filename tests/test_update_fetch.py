import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
import zipfile

from decky_ha import update_check as uc
from decky_ha import update_fetch as uf

from . import ROOT

URL = uc.asset_url("1.2.3")


class FakeResponse:
    def __init__(self, body: bytes, length=None):
        self.stream = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def read(self, n):
        return self.stream.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, "x.zip")

    def run_download(self, body=b"hello", opener=None, **kw):
        return uf.download(URL, self.dest, opener=opener or (lambda url, timeout: FakeResponse(body)), **kw)

    def refused(self, **kw):
        with self.assertRaises(uc.UpdateRefused) as cm:
            self.run_download(**kw)
        self.assertEqual(cm.exception.reason, "download")
        self.assertFalse(os.path.exists(self.dest))  # nothing is left behind

    def test_a_good_download_is_saved_and_verified(self):
        self.assertEqual(self.run_download(b"hello"), 5)
        uf.verify_sha256(self.dest, hashlib.sha256(b"hello").hexdigest())

    def test_a_wrong_hash_is_refused_as_hash(self):
        self.run_download(b"hello")
        with self.assertRaises(uc.UpdateRefused) as cm:
            uf.verify_sha256(self.dest, "0" * 64)
        self.assertEqual(cm.exception.reason, "hash")

    def test_too_large_by_header_or_by_body(self):
        self.refused(opener=lambda u, t: FakeResponse(b"x", length=10_000), cap=100)
        self.refused(body=b"x" * 300, cap=100)

    def test_too_slow(self):
        ticks = iter(range(0, 1000, 30))
        self.refused(body=b"x" * 500_000, timeout=60, clock=lambda: next(ticks))

    def test_network_errors_are_download_failures(self):
        def boom(url, timeout):
            raise urllib.error.URLError("unreachable")
        self.refused(opener=boom)

    def test_only_github_hosts_and_https(self):
        for ok in ("https://github.com/a/b", "https://objects.githubusercontent.com/x", "https://release-assets.githubusercontent.com/x"):
            self.assertTrue(uf.host_allowed(ok), ok)
        for bad in ("http://github.com/a", "https://evil.example/a", "https://github.com.evil.example/a",
                    "https://evilgithubusercontent.com/a", "https://github.com" + chr(64) + "evil.example/a", "file:///etc/passwd"):
            self.assertFalse(uf.host_allowed(bad), bad)
        with self.assertRaises(uc.UpdateRefused):
            uf.download("https://evil.example/x.zip", self.dest, opener=lambda u, t: FakeResponse(b""))

    def test_a_redirect_off_github_is_refused(self):
        handler = uf._Redirects()
        import urllib.request
        req = urllib.request.Request(URL)
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/x.zip")
        self.assertIsNotNone(handler.redirect_request(req, None, 302, "Found", {}, "https://objects.githubusercontent.com/x.zip"))


class SslContextTests(unittest.TestCase):
    def test_the_first_readable_bundle_is_loaded_and_the_rest_are_not_tried(self):
        loaded = []

        def load(self, cafile=None, **kw):
            loaded.append(cafile)
            if cafile == "/bad":
                raise OSError("unreadable")

        with unittest.mock.patch.object(uf.ssl.SSLContext, "load_verify_locations", load):
            context = uf.make_ssl_context(("/bad", "/good", "/never"))
        self.assertEqual(loaded, ["/bad", "/good"])
        self.assertTrue(context.verify_mode == uf.ssl.CERT_REQUIRED and context.check_hostname)

    def test_no_readable_bundle_still_gives_a_verifying_context(self):
        context = uf.make_ssl_context(("/nonexistent/a", "/nonexistent/b"))
        self.assertEqual(context.verify_mode, uf.ssl.CERT_REQUIRED)

    def test_the_default_opener_uses_that_context(self):
        built = []
        real = uf.urllib.request.HTTPSHandler

        class Spy(real):
            def __init__(self, context=None, **kw):
                built.append(context)
                super().__init__(context=context, **kw)

        with unittest.mock.patch.object(uf.urllib.request, "HTTPSHandler", Spy), \
                unittest.mock.patch.object(uf.urllib.request.OpenerDirector, "open", lambda self, *a, **k: "opened"):
            self.assertEqual(uf.default_opener(URL, 5), "opened")
        self.assertEqual(len(built), 1)
        self.assertIsInstance(built[0], uf.ssl.SSLContext)


class RevertCopyTests(unittest.TestCase):
    def make_plugin(self, root, version="1.0.0"):
        for path, text in {"main.py": "x", "plugin.json": "{}", "LICENSE": "l", "package.json": json.dumps({"version": version}),
                           "dist/index.js": "j", "dist/index.js.map": "m", "decky_ha/a.py": "a",
                           "decky_ha/__pycache__/a.pyc": "c", "py_modules/.keep": ""}.items():
            full = os.path.join(root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)

    def test_the_copy_is_a_release_layout_zip_that_passes_the_archive_check(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            self.make_plugin(root)
            target = uf.make_revert_copy(root, data, "1.0.0")
            with zipfile.ZipFile(target) as z:
                names = sorted(z.namelist())
            uc.validate_archive(target, "1.0.0")
        self.assertEqual(names, sorted(f"Decky Home Assistant/{n}" for n in (
            "LICENSE", "decky_ha/a.py", "dist/index.js", "main.py", "package.json", "plugin.json", "py_modules/.keep")))

    def test_only_one_copy_remains(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            self.make_plugin(root, "1.0.0")
            uf.make_revert_copy(root, data, "1.0.0")
            self.make_plugin(root, "1.1.0")
            uf.make_revert_copy(root, data, "1.1.0")
            self.assertEqual(os.listdir(data), ["previous-1.1.0.zip"])

    def test_the_shipped_files_match_the_packager(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import package_plugin
        with tempfile.TemporaryDirectory() as root:
            self.make_plugin(root)
            self.assertEqual(sorted(n for _, n in uf.shipped_files(root)), sorted(n for _, n in package_plugin.collect(root)))
        self.assertEqual((uf.TOP_FILES, uf.TREES, uf.SKIP_PARTS, uf.SKIP_SUFFIXES),
                         (package_plugin.TOP_FILES, package_plugin.TREES, package_plugin.SKIP_PARTS, package_plugin.SKIP_SUFFIXES))

    def test_a_write_error_is_a_failure_not_a_crash(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_plugin(root)
            with self.assertRaises(uc.UpdateRefused):
                uf.make_revert_copy(root, "/proc/nope/forbidden", "1.0.0")
