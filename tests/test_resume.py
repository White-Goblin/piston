import asyncio
import unittest
from unittest import mock

from . import fake as fake_decky, use_temp_settings_dir
from decky_ha import resume as resume_module
from decky_ha.resume import ResumeMixin
from decky_ha.settings import SettingsMixin
from decky_ha.state import StateMixin


class Server:
    def __init__(self, log):
        self.log = log

    def push_state(self, name, data):
        self.log.append(("state", name))

    def push_event(self, name, data=None):
        self.log.append(("event", name))


class Advertiser:
    def __init__(self, log):
        self.log = log

    async def restart(self):
        self.log.append(("mdns", "restart"))


class Host(SettingsMixin, StateMixin, ResumeMixin):
    def __init__(self):
        self.log = []
        self._init_state()
        self.loop = asyncio.get_running_loop()
        self.server = Server(self.log)
        self.advertiser = Advertiser(self.log)
        self.reads = {}
        self.device_value = {"mac": "aa", "ip": "old"}
        self.groups["device"] = dict(self.device_value)      # what was cached before the suspend

    def _resume_reads(self):
        return list(self.reads.items())

    async def _push_device(self):
        self.log.append(("read", "device"))
        self._push("device", self.device_value)


class ResumeRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        use_temp_settings_dir()
        fake_decky.emitted.clear()
        self.h = Host()
        self.network = mock.AsyncMock(return_value=True)
        patch = mock.patch.object(resume_module, "wait_for_network", self.network)
        patch.start()
        self.addCleanup(patch.stop)

    def reader(self, name, value):
        async def read():
            self.h._push(name, value)
        self.h.reads[name] = read

    async def test_a_changed_group_is_pushed_and_resumed_is_the_last_frame(self):
        self.h.groups["audio"] = {"volume": 10}
        self.reader("audio", {"volume": 40})
        await self.h._announce_resume()
        pushes = [e for e in self.h.log if e[0] in ("state", "event")]
        self.assertEqual(pushes, [("state", "audio"), ("event", "resumed")])

    async def test_unchanged_groups_send_nothing_but_resumed_and_the_cache_is_not_replayed(self):
        self.h.groups.update({"audio": {"volume": 10}, "sensors": {"cpu": 1}})   # cached, never re-sent as such
        self.reader("audio", {"volume": 10})
        await self.h._announce_resume()
        self.assertEqual([e for e in self.h.log if e[0] in ("state", "event")], [("event", "resumed")])

    async def test_nothing_is_sent_before_the_network_is_back_then_everything_in_order(self):
        gate = asyncio.Event()

        async def late():
            await gate.wait()
            self.h.log.append(("network", "up"))
            return True

        self.network.side_effect = late
        self.h.groups["audio"] = {"volume": 10}
        self.h.device_value = {"mac": "aa", "ip": "new"}          # the address changed during the suspend
        self.reader("audio", {"volume": 40})
        task = asyncio.create_task(self.h._announce_resume())
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(self.h.log, [])                       # the local read is done but nothing was sent yet
        gate.set()
        await task
        await asyncio.sleep(0)                                 # let the background mDNS restart run
        self.assertEqual(self.h.log[0], ("network", "up"))
        self.assertLess(self.h.log.index(("read", "device")), self.h.log.index(("event", "resumed")))
        self.assertIn(("mdns", "restart"), self.h.log)
        sent = [e for e in self.h.log if e[0] in ("state", "event")]
        self.assertEqual(sent, [("state", "audio"), ("state", "device"), ("event", "resumed")])

    async def test_the_refresh_completes_when_the_network_never_returns(self):
        self.network.return_value = False
        self.reader("audio", {"volume": 40})
        with self.assertLogs("decky-test", "WARNING") as logs:
            await self.h._announce_resume()
            await asyncio.sleep(0)
        self.assertEqual(sum("did not come back" in m for m in logs.output), 1)
        self.assertNotIn(("read", "device"), self.h.log)       # network facts are not read without a network
        self.assertIn(("mdns", "restart"), self.h.log)         # but the advert is still restarted once
        self.assertEqual([e for e in self.h.log if e[0] in ("state", "event")], [("state", "audio"), ("event", "resumed")])

    async def test_one_failing_read_is_logged_once_and_the_others_still_push(self):
        async def broken():
            raise RuntimeError("wpctl gone")

        self.h.reads["audio"] = broken
        self.reader("sensors", {"cpu": 5})
        with self.assertLogs("decky-test", "WARNING") as logs:
            await self.h._announce_resume()
        self.assertEqual(sum("audio failed" in m for m in logs.output), 1)
        self.assertEqual([e for e in self.h.log if e[0] in ("state", "event")], [("state", "sensors"), ("event", "resumed")])

    async def test_a_read_that_hangs_is_skipped_after_the_timeout(self):
        async def hang():
            await asyncio.sleep(30)

        self.h.reads["controllers"] = hang
        self.reader("sensors", {"cpu": 5})
        with mock.patch.object(resume_module, "RESUME_READ_TIMEOUT", 0.05), self.assertLogs("decky-test", "WARNING") as logs:
            await self.h._announce_resume()
        self.assertTrue(any("controllers timed out" in m for m in logs.output))
        self.assertIn(("event", "resumed"), self.h.log)

    async def test_a_cancelled_refresh_releases_the_hold_and_does_not_announce_resumed(self):
        gate = asyncio.Event()

        async def never():
            await gate.wait()
            return True

        self.network.side_effect = never
        self.reader("audio", {"volume": 40})
        task = asyncio.create_task(self.h._announce_resume())
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()                                          # the device went to sleep again
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.h._held)                        # pushes flow normally again
        self.assertNotIn(("event", "resumed"), self.h.log)

    async def test_a_running_picture_in_picture_is_dropped_first(self):
        closed = []
        self.h.pip_sessions = {"sess-12345678": object()}
        self.h._pip_close_all = lambda notify: (closed.append(notify), self.h.pip_sessions.clear())
        await self.h._announce_resume()
        self.assertEqual(closed, [True])
        self.assertIn(("pip", ({"action": "hide"},)), fake_decky.emitted)


class AppHost(Host):
    """A host whose only read is the real running-app sync; the frontend is played by the test."""

    def __init__(self):
        super().__init__()
        self.session_value = "game"
        self.asked = 0

    def _resume_reads(self):
        return [("apps", self._resume_apps)]

    async def _check_session(self):
        self.session = self.session_value

    async def report_app_state(self, appid="", name=""):
        """What ControlMixin.report_app_state does for the resume: a group push and the 'answered' signal."""
        self._push("apps", {"running": bool(appid), "appid": appid})
        self._app_synced.set()


class AppSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        use_temp_settings_dir()
        self.h = AppHost()
        self.h.groups["apps"] = {"running": True, "appid": "42"}          # a game was running before the suspend
        self.answers = []                                                   # what the frontend does per request
        self.requests = []

        async def emit(event, *args):
            if event != "app_sync":
                return
            self.requests.append(event)
            action = self.answers.pop(0) if self.answers else None
            if action is not None:
                await action()

        for patch in (mock.patch.object(fake_decky, "emit", emit),
                      mock.patch.object(resume_module, "wait_for_network", mock.AsyncMock(return_value=True)),
                      mock.patch.object(resume_module, "APP_SYNC_FIRST_WAIT", 0.05),
                      mock.patch.object(resume_module, "APP_SYNC_WINDOW", 0.4),
                      mock.patch.object(resume_module, "APP_SYNC_RETRY", 0.05)):
            patch.start()
            self.addCleanup(patch.stop)

    async def test_a_game_that_ended_during_the_suspend_is_reported_and_pushed(self):
        self.answers = [lambda: self.h.report_app_state("", "")]
        await self.h._announce_resume()
        self.assertEqual(self.requests, ["app_sync"])
        self.assertIn(("state", "apps"), self.h.log)                       # changed: sent
        self.assertEqual(self.h.log[-1], ("event", "resumed"))
        self.assertIsNone(getattr(self.h, "_app_task", None))              # no retry needed

    async def test_a_game_that_is_still_running_changes_nothing(self):
        self.answers = [lambda: self.h.report_app_state("42", "")]
        await self.h._announce_resume()
        self.assertNotIn(("state", "apps"), self.h.log)

    async def test_an_unready_steam_ui_is_pushed_around_then_asked_again_until_it_answers(self):
        self.answers = [None, None, lambda: self.h.report_app_state("", "")]   # silent twice, then ready
        await self.h._announce_resume()
        self.assertEqual(self.h.log[-1], ("event", "resumed"))             # the refresh itself did not wait for it
        await self.h._app_task
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.h.log[-1], ("state", "apps"))                # the late answer is pushed normally

    async def test_it_gives_up_after_the_window_and_logs_once(self):
        with self.assertLogs("decky-test", "WARNING") as logs:
            await self.h._announce_resume()
            await self.h._app_task
        self.assertEqual(sum("did not report the running app" in m for m in logs.output), 1)
        self.assertGreater(len(self.requests), 3)
        self.assertLessEqual(len(self.requests), 10)                       # bounded by the window

    async def test_desktop_mode_does_not_ask_the_steam_ui(self):
        self.h.session_value = "desktop"
        await self.h._announce_resume()
        self.assertEqual(self.requests, [])
        self.assertIsNone(getattr(self.h, "_app_task", None))


if __name__ == "__main__":
    unittest.main()
