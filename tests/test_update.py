import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import decky

from decky_ha import update, update_check, update_fetch
from decky_ha.proto import messages as m

from .test_update_check import make_zip

VERSION = "1.2.3"
URL = update_check.asset_url(VERSION)


class FakeServer:
    def __init__(self):
        self.pushed = []

    def push_update_status(self, state, version="", reason=None):
        self.pushed.append((state, version, reason))


class Host(update.UpdateMixin):
    def __init__(self, loop):
        self.loop = loop
        self.server = FakeServer()
        self._init_update()


class UpdateBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plugin_dir = os.path.join(tmp.name, "plugin")
        self.runtime = os.path.join(tmp.name, "runtime")
        self.settings = os.path.join(tmp.name, "settings")
        for d in (self.plugin_dir, self.runtime, self.settings):
            os.makedirs(d)
        for name, text in {"main.py": "x", "plugin.json": "{}", "package.json": json.dumps({"version": "1.0.0"})}.items():
            with open(os.path.join(self.plugin_dir, name), "w") as f:
                f.write(text)
        for attr, value in (("DECKY_PLUGIN_DIR", self.plugin_dir), ("DECKY_PLUGIN_RUNTIME_DIR", self.runtime),
                            ("DECKY_PLUGIN_SETTINGS_DIR", self.settings)):
            patcher = mock.patch.object(decky, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        decky.emitted.clear()
        self.zip = make_zip(version=VERSION)
        self.sha = hashlib.sha256(self.zip).hexdigest()
        self.downloads = []

        def fake_download(url, dest, **kw):
            self.downloads.append(url)
            with open(dest, "wb") as f:
                f.write(self.zip)
            return len(self.zip)

        patcher = mock.patch.object(update_fetch, "download", fake_download)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = Host(asyncio.get_running_loop())

    def frame(self, **kw):
        return {**m.update(VERSION, URL, self.sha), **kw}

    async def finish(self):
        await self.host.update_task

    def states(self):
        return [s[0] for s in self.host.server.pushed]


class UpdateTests(UpdateBase):

    async def test_a_valid_request_ends_waiting_for_the_ui_with_a_marker_and_a_revert_copy(self):
        self.host._on_update_frame(self.frame())
        self.assertEqual(self.host.update_info["state"], "downloading")
        await self.finish()
        self.assertEqual(self.states(), ["downloading", "waiting_for_ui"])
        self.assertEqual(self.downloads, [URL])
        self.assertEqual(sorted(os.listdir(self.runtime)), ["pending-1.2.3.zip", "previous-1.0.0.zip"])
        with open(os.path.join(self.settings, "update.json")) as f:
            marker = json.load(f)
        self.assertEqual((marker["target"], marker["previous"], marker["confirmed"]), (VERSION, "1.0.0", False))
        self.assertEqual(oct(os.stat(os.path.join(self.settings, "update.json")).st_mode & 0o777), "0o600")
        self.assertEqual(decky.emitted, [("update", (VERSION,))])
        pending = self.host.update_pending_zip
        self.assertEqual((pending["version"], pending["sha256"]), (VERSION, self.sha))
        update_check.validate_archive(pending["path"], VERSION)

    async def test_an_invalid_request_is_refused_before_anything_starts(self):
        for bad in (self.frame(url="https://evil.example/x.zip"), self.frame(version="1.0.0", url=update_check.asset_url("1.0.0")),
                    self.frame(sha256="Z" * 64)):
            with self.assertRaises(m.ProtocolError) as cm:
                self.host._on_update_frame(bad)
            self.assertEqual(cm.exception.code, m.BAD_REQUEST)
        self.assertEqual(self.host.server.pushed, [])
        self.assertEqual(self.downloads, [])

    async def test_a_second_request_while_busy_is_refused(self):
        self.host._on_update_frame(self.frame())
        with self.assertRaises(m.ProtocolError) as cm:
            self.host._on_update_frame(self.frame())
        self.assertEqual(cm.exception.code, m.BUSY)
        await self.finish()

    async def test_a_wrong_hash_fails_and_leaves_nothing(self):
        self.host._on_update_frame(self.frame(sha256="0" * 64))
        await self.finish()
        self.assertEqual(self.host.server.pushed[-1], ("failed", VERSION, "hash"))
        self.assertEqual(os.listdir(self.runtime), [])
        self.assertFalse(os.path.exists(os.path.join(self.settings, "update.json")))
        self.assertIsNone(self.host.update_pending_zip)
        self.assertEqual(decky.emitted, [])

    async def test_a_bad_archive_fails_as_archive(self):
        self.zip = make_zip(version="9.9.9")       # the zip holds another version than the request names
        self.sha = hashlib.sha256(self.zip).hexdigest()
        self.host._on_update_frame(self.frame())
        await self.finish()
        self.assertEqual(self.host.server.pushed[-1], ("failed", VERSION, "archive"))
        self.assertEqual(os.listdir(self.runtime), [])

    async def test_a_download_error_fails_as_download(self):
        def broken(url, dest, **kw):
            raise update_check.UpdateRefused("download", "unreachable")
        with mock.patch.object(update_fetch, "download", broken):
            self.host._on_update_frame(self.frame())
            await self.finish()
        self.assertEqual(self.host.server.pushed[-1], ("failed", VERSION, "download"))

    async def test_an_unexpected_error_is_a_failure_not_a_crash(self):
        with mock.patch.object(update_fetch, "make_revert_copy", side_effect=RuntimeError("boom")):
            self.host._on_update_frame(self.frame())
            await self.finish()
        self.assertEqual(self.host.server.pushed[-1][0], "failed")

    async def test_after_a_failure_a_new_request_is_accepted(self):
        self.host._on_update_frame(self.frame(sha256="0" * 64))
        await self.finish()
        self.host._on_update_frame(self.frame())
        await self.finish()
        self.assertEqual(self.states()[-1], "waiting_for_ui")

    async def test_the_status_is_what_a_lane_greeting_and_a_new_connection_report(self):
        self.assertEqual(self.host._update_status(), {"state": "idle", "version": ""})
        self.host._resend_update_status()
        self.assertEqual(self.host.server.pushed, [])            # idle: nothing to say
        self.host._on_update_frame(self.frame(sha256="0" * 64))
        await self.finish()
        self.assertEqual(self.host._update_status(), {"state": "failed", "version": VERSION, "reason": "hash"})
        self.host.server.pushed.clear()
        self.host._resend_update_status()
        self.assertEqual(self.host.server.pushed, [("failed", VERSION, "hash")])


class RouteTests(UpdateBase):
    """The one-shot zip route and the frontend callables, on top of the same fixtures."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from decky_ha.http_server import HttpServerMixin

        class Both(update.UpdateMixin, HttpServerMixin):
            def __init__(self, loop):
                self.loop, self.server, self.http_port = loop, FakeServer(), 47821
                self._init_update()

        self.host = Both(asyncio.get_running_loop())
        self.host._on_update_frame(self.frame())
        await self.host.update_task

    async def get(self, path, host="127.0.0.1:47821"):
        response = await self.host._handle_request("GET", path, b"", {"host": host})
        head, _, body = response.partition(b"\r\n\r\n")
        return head.split(b"\r\n")[0].decode(), body

    async def test_the_zip_is_served_once_with_the_right_bytes(self):
        pending = await self.host.update_pending()
        self.assertEqual(pending["version"], VERSION)
        self.assertEqual(pending["sha256"], self.sha)
        path = "/" + pending["url"].split("/", 3)[3]
        status, body = await self.get(path)
        self.assertEqual((status, body), ("HTTP/1.1 200 OK", self.zip))
        self.assertEqual((await self.get(path))[0], "HTTP/1.1 404 Not Found")       # single use

    async def test_a_new_ask_allows_another_download(self):
        path = "/" + (await self.host.update_pending())["url"].split("/", 3)[3]
        await self.get(path)
        await self.host.update_pending()
        self.assertEqual((await self.get(path))[0], "HTTP/1.1 200 OK")

    async def test_wrong_nonce_wrong_host_and_wrong_names_get_404(self):
        pending = await self.host.update_pending()
        nonce = pending["url"].split("/")[4]
        for path, host in ((f"/update/{'x' * len(nonce)}/plugin.zip", "127.0.0.1:47821"),
                           (f"/update/{nonce}/plugin.zip", "evil.example"), (f"/update/{nonce}/plugin.zip", "127.0.0.1:1"),
                           (f"/update/{nonce}/other.zip", "127.0.0.1:47821"), (f"/update/{nonce}", "127.0.0.1:47821")):
            self.assertEqual((await self.get(path, host))[0], "HTTP/1.1 404 Not Found", (path, host))

    async def test_nothing_is_offered_unless_an_update_is_waiting(self):
        self.host._drop_pending()
        self.assertIsNone(await self.host.update_pending())
        self.assertEqual((await self.get("/update/abc/plugin.zip"))[0], "HTTP/1.1 404 Not Found")

    async def test_an_expired_update_is_dropped_and_the_status_goes_idle(self):
        self.host.update_pending_zip["expires"] = 0
        self.assertIsNone(await self.host.update_pending())
        self.assertEqual(self.host.update_info["state"], "idle")
        self.assertEqual(sorted(os.listdir(self.runtime)), ["previous-1.0.0.zip"])
        self.assertFalse(os.path.exists(os.path.join(self.settings, "update.json")))

    async def test_report_accepted_moves_to_installing(self):
        await self.host.update_report({"result": "accepted"})
        self.assertEqual(self.host.server.pushed[-1], ("installing", VERSION, None))

    async def test_report_error_fails_as_installer_and_cleans_up(self):
        await self.host.update_report({"result": "error", "detail": "refused"})
        self.assertEqual(self.host.server.pushed[-1], ("failed", VERSION, "installer"))
        self.assertIsNone(self.host.update_pending_zip)
        self.assertFalse(os.path.exists(os.path.join(self.settings, "update.json")))

    async def test_report_without_a_waiting_update_is_ignored(self):
        self.host._drop_pending()
        self.host.update_info = {"state": "idle", "version": "", "reason": None}
        before = list(self.host.server.pushed)
        await self.host.update_report({"result": "error"})
        self.assertEqual(self.host.server.pushed, before)


class StartupTests(UpdateBase):
    """What the next start does with the marker an update left behind."""

    def write_marker(self, **kw):
        marker = {"target": VERSION, "previous": "1.0.0", "sha256": self.sha, "state": "installing",
                  "expires": 9e12, "deadline": 9e12, "confirmed": False}
        marker.update(kw)
        from decky_ha.settings import write_json_atomic
        write_json_atomic(os.path.join(self.settings, "update.json"), marker)

    def set_installed(self, version):
        with open(os.path.join(self.plugin_dir, "package.json"), "w") as f:
            json.dump({"version": version}, f)

    def marker_exists(self):
        return os.path.exists(os.path.join(self.settings, "update.json"))

    async def test_no_marker_means_nothing_happens(self):
        self.host._update_startup()
        self.assertEqual(self.host.update_info["state"], "idle")

    async def test_the_new_version_is_confirmed_after_the_healthy_interval(self):
        self.set_installed(VERSION)
        self.write_marker()
        self.host.server.port = 47830
        with mock.patch.object(update, "HEALTHY_AFTER", 0.05):
            self.host._update_startup()
            self.assertTrue(self.marker_exists())               # not yet
            await self.host.update_confirm_task
        self.assertFalse(self.marker_exists())
        self.assertEqual(self.host.update_info["state"], "idle")

    async def test_a_server_that_is_not_listening_stays_unconfirmed(self):
        self.set_installed(VERSION)
        self.write_marker()
        self.host.server.port = 0
        with mock.patch.object(update, "HEALTHY_AFTER", 0.01):
            self.host._update_startup()
            await self.host.update_confirm_task
        self.assertTrue(self.marker_exists())

    async def test_an_install_that_did_not_happen_is_reported_and_cleaned_up(self):
        self.write_marker(state="installing")                   # still on 1.0.0
        with open(os.path.join(self.runtime, f"pending-{VERSION}.zip"), "wb") as f:
            f.write(b"x")
        self.host._update_startup()
        self.assertEqual(self.host._update_status(), {"state": "failed", "version": VERSION, "reason": "installer"})
        self.assertFalse(self.marker_exists())
        self.assertEqual(os.listdir(self.runtime), [])

    async def test_a_restart_while_waiting_offers_the_same_verified_zip_again(self):
        self.write_marker(state="waiting_for_ui")
        with open(os.path.join(self.runtime, f"pending-{VERSION}.zip"), "wb") as f:
            f.write(self.zip)
        self.host._update_startup()
        await asyncio.sleep(0)
        self.assertEqual(self.host.update_info["state"], "waiting_for_ui")
        self.assertEqual(self.host.update_pending_zip["version"], VERSION)
        self.assertEqual(decky.emitted, [("update", (VERSION,))])

    async def test_an_expired_waiting_marker_is_dropped_as_failed(self):
        self.write_marker(state="waiting_for_ui", expires=1)
        with open(os.path.join(self.runtime, f"pending-{VERSION}.zip"), "wb") as f:
            f.write(self.zip)
        self.host._update_startup()
        self.assertEqual(self.host.update_info["state"], "failed")
        self.assertFalse(self.marker_exists())

    async def test_the_marker_callable_returns_it(self):
        self.assertIsNone(await self.host.update_marker())
        self.write_marker()
        self.assertEqual((await self.host.update_marker())["target"], VERSION)
        with open(os.path.join(self.settings, "update.json"), "w") as f:
            f.write("not json")
        self.assertIsNone(await self.host.update_marker())

    async def test_accepted_persists_installing_with_a_fresh_deadline(self):
        self.host._on_update_frame(self.frame())
        await self.finish()
        await self.host.update_report({"result": "accepted"})
        with open(os.path.join(self.settings, "update.json")) as f:
            marker = json.load(f)
        self.assertEqual(marker["state"], "installing")
        self.assertLess(marker["deadline"] - __import__("time").time(), update.REVERT_WINDOW + 1)
