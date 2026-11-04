import base64
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from piston_device import artwork as aw
from piston_device.proto import messages as m

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 100


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/ok.jpg":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(JPEG)
        elif self.path == "/big":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"\xff\xd8\xff" + b"x" * (aw.MAX_BYTES + 10))
        elif self.path == "/slow":
            time.sleep(1.5)
            self.send_response(200)
            self.end_headers()
        elif self.path == "/to-file":
            self.send_response(302)
            self.send_header("Location", "file:///etc/hostname")
            self.end_headers()
        elif self.path == "/to-ok":
            self.send_response(302)
            self.send_header("Location", "/ok.jpg")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class UrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_data_url(self):
        art = aw.load_from_url("data:image/jpeg;base64," + base64.b64encode(JPEG).decode())
        self.assertEqual((art.content_type, art.data), ("image/jpeg", JPEG))
        again = aw.load_from_url("data:image/jpeg;base64," + base64.b64encode(JPEG).decode())
        self.assertEqual(art.id, again.id)
        self.assertEqual(len(art.id), 16)

    def test_the_type_comes_from_the_bytes_not_the_url(self):
        art = aw.load_from_url("data:image/jpeg;base64," + base64.b64encode(PNG).decode())
        self.assertEqual(art.content_type, "image/png")

    def test_bad_data_urls(self):
        big = base64.b64encode(b"\xff\xd8\xff" + b"x" * (aw.MAX_BYTES + 10)).decode()
        for url in ("data:image/jpeg;base64," + big, "data:text/plain;base64," + base64.b64encode(b"hello").decode(),
                    "data:image/jpeg;base64,", "data:image/jpeg;base64,@@@@", "data:", "", None, "ftp://x/y.jpg", "javascript:1"):
            self.assertIsNone(aw.load_from_url(url), msg=repr(url)[:60])

    def test_file_url(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".org.chromium.Chromium.abc")
            with open(path, "wb") as f:
                f.write(PNG)
            self.assertEqual(aw.load_from_url("file://" + path).content_type, "image/png")
            self.assertIsNone(aw.load_from_url("file://" + os.path.join(d, "missing")))
            self.assertIsNone(aw.load_from_url("file://" + d))                       # a directory
            self.assertIsNone(aw.load_from_url("file://otherhost" + path))           # not this machine

    def test_http(self):
        self.assertEqual(aw.load_from_url(self.base + "/ok.jpg").data, JPEG)
        self.assertEqual(aw.load_from_url(self.base + "/to-ok").data, JPEG)           # redirects to http are fine
        self.assertIsNone(aw.load_from_url(self.base + "/missing"))
        self.assertIsNone(aw.load_from_url(self.base + "/big"))                      # over the cap

    def test_redirect_to_a_file_is_refused(self):
        self.assertIsNone(aw.load_from_url(self.base + "/to-file"))

    def test_a_slow_server_times_out(self):
        start = time.monotonic()
        self.assertIsNone(aw.load_from_url(self.base + "/slow", timeout=0.3))
        self.assertLess(time.monotonic() - start, 1.2)

    def test_refused_connection(self):
        self.assertIsNone(aw.load_from_url("http://127.0.0.1:9/x.jpg", timeout=0.5))


class SteamTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)

    def write(self, rel, data):
        path = os.path.join(self.root.name, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def test_shortcut_prefers_portrait_then_wide(self):
        self.write("userdata/1/config/grid/42.jpg", JPEG)
        self.assertEqual(aw.load_steam("42", self.root.name).data, JPEG)
        self.write("userdata/1/config/grid/42p.png", PNG)
        self.assertEqual(aw.load_steam("42", self.root.name).data, PNG)

    def test_the_user_directory_without_the_file_is_skipped(self):
        os.makedirs(os.path.join(self.root.name, "userdata", "0", "config", "grid"))
        self.write("userdata/2/config/grid/42p.png", PNG)
        self.assertEqual(aw.load_steam("42", self.root.name).data, PNG)

    def test_library_cache_layouts(self):
        self.write("appcache/librarycache/620/header.jpg", JPEG)
        self.assertEqual(aw.load_steam("620", self.root.name).content_type, "image/jpeg")
        self.write("appcache/librarycache/620/library_600x900.jpg", PNG)
        self.assertEqual(aw.load_steam("620", self.root.name).data, PNG)
        self.write("appcache/librarycache/730/f9/library_header.jpg", JPEG)
        self.assertEqual(aw.load_steam("730", self.root.name).data, JPEG)

    def test_nothing_found_or_bad_appid(self):
        self.assertIsNone(aw.load_steam("42", self.root.name))
        self.assertIsNone(aw.load_steam("../42", self.root.name))
        self.assertIsNone(aw.load_steam("42", os.path.join(self.root.name, "nope")))

    def test_a_non_image_file_is_skipped_for_the_next_candidate(self):
        self.write("userdata/1/config/grid/42p.png", b"not an image")
        self.write("userdata/1/config/grid/42.jpg", JPEG)
        self.assertEqual(aw.load_steam("42", self.root.name).data, JPEG)


class StoreTests(unittest.TestCase):
    def test_chunks_reassemble(self):
        data = b"\xff\xd8\xff" + os.urandom(m.ARTWORK_CHUNK_BYTES * 2 + 123)
        art = aw.make_art(data)
        store = aw.ArtStore()
        store.set(art)
        got, offset, done = b"", 0, False
        while not done:
            content_type, size, piece, done = store.chunk(art.id, offset)
            self.assertLessEqual(len(piece), m.ARTWORK_CHUNK_BYTES)
            self.assertEqual((content_type, size), ("image/jpeg", len(data)))
            got += piece
            offset += len(piece)
        self.assertEqual(got, data)

    def test_unknown_id_and_bad_offset(self):
        store = aw.ArtStore()
        with self.assertRaises(LookupError):
            store.chunk("abc")
        art = aw.make_art(JPEG)
        store.set(art)
        with self.assertRaises(LookupError):
            store.chunk("other")
        for offset in (-1, len(JPEG), 10 ** 6):
            with self.assertRaises(ValueError):
                store.chunk(art.id, offset)
        store.set(None)
        self.assertIsNone(store.id)

    def test_the_cap(self):
        self.assertIsNone(aw.make_art(b"\xff\xd8\xff" + b"x" * aw.MAX_BYTES))
        self.assertIsNotNone(aw.make_art(b"\xff\xd8\xff" + b"x" * (aw.MAX_BYTES - 3)))


if __name__ == "__main__":
    unittest.main()
