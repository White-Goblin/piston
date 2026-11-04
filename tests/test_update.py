import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import decky

from piston_device import update, update_check, update_fetch
from piston_device.proto import messages as m

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


class PendingTests(UpdateBase):
    """What the frontend is told to install (file:// URLs) and how it reports back."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.host._on_update_frame(self.frame())
        await self.host.update_task

    async def test_the_frontend_gets_file_urls_and_the_hashes_of_the_files(self):
        pending = await self.host.update_pending()
        self.assertEqual((pending["version"], pending["sha256"], pending["previous"]), (VERSION, self.sha, "1.0.0"))
        self.assertEqual(pending["url"], "file://" + os.path.join(self.runtime, f"pending-{VERSION}.zip"))
        self.assertEqual(pending["revert_url"], "file://" + os.path.join(self.runtime, "previous-1.0.0.zip"))
        for url, expected in ((pending["url"], pending["sha256"]), (pending["revert_url"], pending["revert_sha256"])):
            self.assertTrue(url.startswith("file:///"))
            self.assertEqual(update_fetch.sha256_of(url[len("file://"):]), expected)

    async def test_the_url_is_not_percent_encoded_even_when_the_path_has_spaces(self):
        self.host._drop_pending()
        spaced = os.path.join(self.runtime, "Piston")
        os.makedirs(spaced)
        with mock.patch.object(update.UpdateMixin, "_update_dir", lambda self: spaced):
            self.host.update_info = {"state": "idle", "version": "", "reason": None}
            self.host._on_update_frame(self.frame())
            await self.host.update_task
            pending = await self.host.update_pending()
        self.assertIn("Piston", pending["url"])
        self.assertNotIn("%", pending["url"])

    async def test_the_marker_keeps_the_revert_facts_for_a_dead_backend(self):
        with open(os.path.join(self.settings, "update.json")) as f:
            marker = json.load(f)
        pending = await self.host.update_pending()
        self.assertEqual((marker["revert_path"], marker["revert_sha256"]), (pending["revert_url"][len("file://"):], pending["revert_sha256"]))

    async def test_nothing_is_offered_unless_an_update_is_waiting(self):
        self.host._drop_pending()
        self.assertIsNone(await self.host.update_pending())

    async def test_an_expired_update_is_dropped_and_the_status_goes_idle(self):
        self.host.update_pending_zip["expires"] = 0
        self.assertIsNone(await self.host.update_pending())
        self.assertEqual(self.host.update_info["state"], "idle")
        self.assertEqual(sorted(os.listdir(self.runtime)), ["previous-1.0.0.zip"])
        self.assertFalse(os.path.exists(os.path.join(self.settings, "update.json")))

    async def test_report_requested_waits_for_the_users_approval(self):
        await self.host.update_report({"result": "requested"})
        self.assertEqual(self.host.server.pushed[-1], ("waiting_for_confirmation", VERSION, None))
        with open(os.path.join(self.settings, "update.json")) as f:
            marker = json.load(f)
        self.assertEqual(marker["state"], "waiting_for_confirmation")
        self.assertGreater(marker["confirm_by"], __import__("time").time() + update.CONFIRM_WINDOW - 5)
        self.assertIsNone(await self.host.update_pending())          # not offered again while the dialog waits
        self.host._drop_pending()

    async def test_a_fresh_interface_can_ask_for_the_update_again_while_the_dialog_is_believed_open(self):
        await self.host.update_report({"result": "requested"})
        self.assertIsNone(await self.host.update_pending())                      # the normal check does not repeat a request
        again = await self.host.update_pending(True)                              # a freshly loaded interface does
        self.assertEqual((again["version"], again["sha256"]), (VERSION, self.sha))
        self.host.server.pushed.clear()
        await self.host.update_report({"result": "requested"})                    # the new request: same state, no new status frame
        self.assertEqual(self.host.update_info["state"], "waiting_for_confirmation")
        self.assertEqual(self.host.server.pushed, [])
        self.host._drop_pending()

    async def test_a_new_request_gives_the_user_a_full_window_again(self):
        with mock.patch.object(update, "CONFIRM_WINDOW", 0.3):
            await self.host.update_report({"result": "requested"})
            first = self.host.update_approval_task
            await asyncio.sleep(0.2)
            await self.host.update_report({"result": "requested"})
            self.assertTrue(first.cancelled() or first.cancelling())
            await asyncio.sleep(0.2)                                              # 0.4 s after the first request, 0.2 after the second
            self.assertEqual(self.host.update_info["state"], "waiting_for_confirmation")
            await self.host.update_approval_task
        self.assertEqual(self.host.server.pushed[-1], ("failed", VERSION, "not_confirmed"))

    async def test_requested_is_not_offered_when_nothing_is_pending(self):
        self.host._drop_pending()
        self.assertIsNone(await self.host.update_pending(True))

    async def test_an_ignored_or_cancelled_dialog_ends_as_not_confirmed(self):
        with mock.patch.object(update, "CONFIRM_WINDOW", 0.05):
            await self.host.update_report({"result": "requested"})
            await self.host.update_approval_task
        self.assertEqual(self.host.server.pushed[-1], ("failed", VERSION, "not_confirmed"))
        self.assertEqual(sorted(os.listdir(self.runtime)), ["previous-1.0.0.zip"])
        self.assertFalse(os.path.exists(os.path.join(self.settings, "update.json")))
        self.assertIsNone(self.host.update_pending_zip)

    async def test_a_new_version_arriving_stops_the_wait(self):
        await self.host.update_report({"result": "requested"})
        task = self.host.update_approval_task
        self.host._drop_pending()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())

    async def test_a_second_update_is_busy_while_the_dialog_waits(self):
        await self.host.update_report({"result": "requested"})
        with self.assertRaises(m.ProtocolError) as cm:
            self.host._on_update_frame(self.frame())
        self.assertEqual(cm.exception.code, m.BUSY)
        self.host._drop_pending()

    async def test_the_old_accepted_result_is_no_longer_a_thing(self):
        await self.host.update_report({"result": "accepted"})          # treated like an error
        self.assertEqual(self.host.server.pushed[-1], ("failed", VERSION, "installer"))

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

    async def test_the_http_server_no_longer_serves_updates(self):
        from piston_device.http_server import HttpServerMixin

        class Both(HttpServerMixin):
            nonce = "n"

        response = await Both()._handle_request("GET", "/update/abc/plugin.zip")
        self.assertTrue(response.startswith(b"HTTP/1.1 404"))


class StartupTests(UpdateBase):
    """What the next start does with the marker an update left behind."""

    def write_marker(self, **kw):
        marker = {"target": VERSION, "previous": "1.0.0", "sha256": self.sha, "state": "installing",
                  "expires": 9e12, "deadline": 9e12, "confirmed": False,
                  "revert_path": "/data/previous-1.0.0.zip", "revert_sha256": "b" * 64}
        marker.update(kw)
        from piston_device.settings import write_json_atomic
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
        self.assertEqual((self.host.update_pending_zip["revert_path"], self.host.update_pending_zip["revert_sha256"]),
                         ("/data/previous-1.0.0.zip", "b" * 64))
        self.assertEqual(decky.emitted, [("update", (VERSION,))])

    async def test_a_restart_with_the_dialog_still_within_its_window_offers_the_zip_again(self):
        import time
        self.write_marker(state="waiting_for_confirmation", confirm_by=time.time() + 600)
        with open(os.path.join(self.runtime, f"pending-{VERSION}.zip"), "wb") as f:
            f.write(self.zip)
        self.host._update_startup()
        self.assertEqual(self.host.update_info["state"], "waiting_for_ui")
        self.assertEqual((await self.host.update_pending())["version"], VERSION)

    async def test_a_restart_after_the_window_reports_not_confirmed(self):
        self.write_marker(state="waiting_for_confirmation", confirm_by=1)
        with open(os.path.join(self.runtime, f"pending-{VERSION}.zip"), "wb") as f:
            f.write(self.zip)
        self.host._update_startup()
        self.assertEqual(self.host._update_status(), {"state": "failed", "version": VERSION, "reason": "not_confirmed"})
        self.assertFalse(self.marker_exists())
        self.assertEqual(os.listdir(self.runtime), [])

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
