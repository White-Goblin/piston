import asyncio
import unittest
from unittest import mock

from . import fake as fake_decky, use_temp_settings_dir
from decky_ha.audio import AudioMixin
from decky_ha.control import ControlMixin
from decky_ha.pip import PipMixin
from decky_ha.pip_signal import PipSignalMixin
from decky_ha.proto import messages as m
from decky_ha.settings import SettingsMixin
from decky_ha.state import StateMixin


class Host(SettingsMixin, StateMixin, ControlMixin, AudioMixin, PipMixin, PipSignalMixin):
    def __init__(self):
        self._init_state()
        self._init_pip_signal()
        self.app_state = {"running": False, "appid": "", "name": "", "screen": "steam"}
        self.app_focus = ""
        self.app_reported = False
        self.sensor_wake = asyncio.Event()
        self.pushed, self.events = [], []
        self.http_port, self.nonce = 0, "n"
        self.calls = []

    def _push(self, name, data):
        self.pushed.append((name, data))
        super()._push(name, data)

    def _push_event(self, name, data=None):
        self.events.append((name, data))

    def _busctl(self, *args):
        self.calls.append(("busctl", args))
        if args[0] == "get-property":
            return {"Active": "b true", "AudioLogicalAddress": "y 5"}[args[-1]]
        return ""

    def _wpctl(self, *args):
        self.calls.append(("wpctl", args))
        if args[0] == "get-volume":
            return "Volume: 0.40"
        if args[0] == "inspect":
            return 'node.name = "alsa_output.hdmi-stereo"'
        return ""


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        use_temp_settings_dir()
        fake_decky.emitted.clear()
        self.h = Host()
        self.h.loop = asyncio.get_running_loop()
        self.h._update_settings({"apps": [{"appid": "250900", "name": "Isaac", "kind": "steam"},
                                          {"appid": "42", "name": "Other", "kind": "steam"}]})

    def emitted(self, name):
        return [a[0] for n, a in fake_decky.emitted if n == name]

    # --- apps ---------------------------------------------------------------

    async def test_launch_validates_and_requires_an_offered_app(self):
        for data in ({}, {"appid": "abc"}, {"appid": "999"}):
            with self.assertRaises(m.ProtocolError) as cm:
                await self.h._cmd_launch(data)
            self.assertEqual(cm.exception.code, m.BAD_REQUEST)

    async def test_launch_when_nothing_runs(self):
        async def running():
            await asyncio.sleep(0.05)
            await self.h.report_app_state("250900", "Isaac")
        asyncio.create_task(running())
        self.assertEqual(await self.h._cmd_launch({"appid": "250900"}), {"started": True})
        self.assertEqual(self.emitted("app"), [{"action": "launch", "appid": "250900"}])

    async def test_launch_running_app_is_a_noop(self):
        await self.h.report_app_state("250900", "Isaac")
        fake_decky.emitted.clear()
        self.assertTrue((await self.h._cmd_launch({"appid": "250900"}))["already_running"])
        self.assertEqual(self.emitted("app"), [])

    async def test_launch_closes_the_old_app_first(self):
        await self.h.report_app_state("42", "Other")

        async def follow():
            await asyncio.sleep(0.05)
            await self.h.report_app_state("", "")
            await asyncio.sleep(0.05)
            await self.h.report_app_state("250900", "Isaac")
        asyncio.create_task(follow())
        await self.h._cmd_launch({"appid": "250900"})
        self.assertEqual(self.emitted("app"), [{"action": "quit", "appid": "42"}, {"action": "launch", "appid": "250900"}])

    async def test_launch_fails_if_old_app_never_closes(self):
        await self.h.report_app_state("42", "Other")
        with mock.patch("decky_ha.control.APP_WAIT_SECONDS", 0.2):
            with self.assertRaises(m.ProtocolError) as cm:
                await self.h._cmd_launch({"appid": "250900"})
        self.assertEqual(cm.exception.code, m.INTERNAL)
        self.assertEqual(self.emitted("app"), [{"action": "quit", "appid": "42"}])

    async def test_quit_with_nothing_running_is_ok(self):
        self.assertEqual(await self.h._cmd_quit({}), {})
        self.assertEqual(self.emitted("app"), [])

    async def test_screen_and_apps_group(self):
        await self.h.report_app_state("250900", "Isaac")
        self.assertEqual(self.h.groups["apps"]["screen"], "game")
        await self.h.report_app_focus("steam")
        self.assertEqual(self.h.groups["apps"]["screen"], "steam_over_game")
        await self.h.report_app_state("", "")
        self.assertEqual(self.h.groups["apps"]["screen"], "steam")
        self.assertEqual(len(self.h.groups["apps"]["apps"]), 2)

    async def test_no_apps_group_before_the_frontend_reports(self):
        self.assertNotIn("apps", self.h.groups)

    # --- power --------------------------------------------------------------

    async def test_power_rejects_unknown_action(self):
        with self.assertRaises(m.ProtocolError):
            await self.h._cmd_power({"action": "explode"})

    async def test_suspend_announces_sleep_before_emitting(self):
        with mock.patch("decky_ha.control.SLEEP_FLUSH_SECONDS", 0):
            await self.h._cmd_power({"action": "suspend"})
        self.assertEqual(self.h.events[0][0], "going_to_sleep")
        self.assertEqual(self.emitted("power"), [{"action": "suspend"}])

    async def test_suspend_carries_the_reason(self):
        with mock.patch("decky_ha.control.SLEEP_FLUSH_SECONDS", 0):
            await self.h._cmd_power({"action": "suspend"})
        self.assertEqual(self.h.events, [("going_to_sleep", {"reason": "suspend"})])

    async def test_suspend_does_not_announce_itself_when_logind_does(self):
        self.h.lifecycle = mock.Mock(active=True, protecting=True)
        await self.h._cmd_power({"action": "suspend"})         # no SLEEP_FLUSH patch needed: it must not wait either
        self.assertEqual(self.h.events, [])
        self.assertEqual(self.emitted("power"), [{"action": "suspend"}])

    async def test_suspend_announces_itself_when_logind_cannot_protect_it(self):
        self.h.lifecycle = mock.Mock(active=True, protecting=False)
        with mock.patch("decky_ha.control.SLEEP_FLUSH_SECONDS", 0):
            await self.h._cmd_power({"action": "suspend"})
        self.assertEqual([e[0] for e in self.h.events], ["going_to_sleep"])

    async def test_restart_actions_are_forwarded(self):
        for action in ("restart", "shutdown", "restart_steam"):
            await self.h._cmd_power({"action": action})
        self.assertEqual([p["action"] for p in self.emitted("power")], ["restart", "shutdown", "restart_steam"])

    async def test_desktop_and_game_switch_the_session(self):
        with mock.patch.object(Host, "_switch_session") as sw:
            await self.h._cmd_power({"action": "desktop"})
            await self.h._cmd_power({"action": "game"})
        self.assertEqual([c.args for c in sw.call_args_list], [(True,), (False,)])

    # --- toast / sensor interval ---------------------------------------------

    async def test_toast_forwarded_with_defaults(self):
        await self.h._cmd_toast({"message": "Hi", "icon": "info", "id": 7})
        self.assertEqual(self.emitted("notification"), [{"title": "Home Assistant", "message": "Hi", "duration": 5000,
                                                         "silent": False, "icon": "info", "id": "7"}])

    async def test_toast_rejects_bad_input(self):
        for data in ({}, {"message": " "}, {"message": "x" * 501}, {"message": "x", "duration": -1},
                     {"message": "x", "duration": "a"}, {"message": "x", "icon": "../x"}):
            with self.assertRaises(m.ProtocolError, msg=str(data)):
                await self.h._cmd_toast(data)

    async def test_sensor_interval(self):
        await self.h._cmd_set_sensor_interval({"seconds": 60})
        self.assertEqual(self.h._load_settings()["sensor_interval"], 60)
        self.assertTrue(self.h.sensor_wake.is_set())
        with self.assertRaises(m.ProtocolError):
            await self.h._cmd_set_sensor_interval({"seconds": 7})

    async def test_toast_click_event(self):
        await self.h.report_toast_click("a")
        self.assertEqual(self.h.events[0][0], "toast_click")
        self.assertEqual(self.h.events[0][1]["id"], "a")

    # --- audio ---------------------------------------------------------------

    async def test_cec_volume_steps_and_mute(self):
        self.h.audio_mode = "cec"
        await self.h._cmd_audio({"action": "volume_up", "steps": 2})
        presses = [c for c in self.h.calls if c[1][0] == "call"]
        self.assertEqual(len(presses), 2)
        self.assertIn("VolumeUp", presses[0][1])
        await self.h._cmd_audio({"action": "mute"})
        self.assertEqual(len([c for c in self.h.calls if c[1][0] == "call"]), 3)

    async def test_cec_rejects_system_actions_and_bad_steps(self):
        self.h.audio_mode = "cec"
        with self.assertRaises(m.ProtocolError):
            await self.h._cmd_audio({"action": "set_volume", "value": 10})
        with self.assertRaises(m.ProtocolError):
            await self.h._cmd_audio({"action": "volume_up", "steps": 99})

    async def test_system_volume_and_mute_push_the_audio_group(self):
        self.h.audio_mode = "system"
        await self.h._cmd_audio({"action": "set_volume", "value": 55})
        self.assertIn(("wpctl", ("set-volume", "@DEFAULT_AUDIO_SINK@", "0.55")), self.h.calls)
        await self.h._cmd_audio({"action": "set_mute", "value": True})
        self.assertEqual(self.h.groups["audio"], {"mode": "system", "volume": 40, "muted": False})

    async def test_system_mode_rejects_cec_actions_and_bad_values(self):
        self.h.audio_mode = "system"
        for data, code in (({"action": "volume_up"}, m.UNSUPPORTED), ({"action": "set_volume", "value": 101}, m.BAD_REQUEST),
                           ({"action": "set_mute", "value": "yes"}, m.BAD_REQUEST), ({"action": "x"}, m.UNSUPPORTED)):
            with self.assertRaises(m.ProtocolError) as cm:
                await self.h._cmd_audio(data)
            self.assertEqual(cm.exception.code, code, data)

    async def test_audio_mode_detection(self):
        self.assertTrue(await self.h._detect_audio_mode())
        self.assertEqual(self.h.audio_mode, "cec")

    # --- pip -----------------------------------------------------------------

    async def test_pip_show_defaults_and_validation(self):
        await self.h._cmd_pip_show({"camera": "camera.doorbell", "session": "sess-0001"})
        self.assertEqual(self.emitted("pip")[0], {"action": "show", "entity_id": "camera.doorbell", "session": "sess-0001",
                                                  "position": "top-right", "size": 0.4, "duration": 30000, "mute": True})
        for data in ({}, {"camera": "light.x"}, {"camera": "camera.a", "position": "middle", "session": "sess-0001"},
                     {"camera": "camera.a", "size": 2, "session": "sess-0001"}, {"camera": "camera.a", "duration": -5, "session": "sess-0001"},
                     {"camera": "camera.a"}, {"camera": "camera.a", "session": "x"}):
            with self.assertRaises(m.ProtocolError, msg=str(data)):
                await self.h._cmd_pip_show(data)

    async def test_pip_hide_and_state_group(self):
        await self.h._cmd_pip_hide({})
        self.assertEqual(self.emitted("pip")[-1], {"action": "hide"})
        await self.h.report_pip_state("playing", "camera.a")
        self.assertEqual(self.h.groups["pip"]["state"], "playing")


class DesktopModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        use_temp_settings_dir()
        fake_decky.emitted.clear()
        self.h = Host()
        self.h.loop = asyncio.get_running_loop()
        self.h.session = "desktop"
        self.h._update_settings({"apps": [{"appid": "42", "name": "Other", "kind": "steam"}]})

    async def test_frontend_dependent_commands_are_refused_with_a_clear_reason(self):
        for coro in (self.h._cmd_launch({"appid": "42"}), self.h._cmd_quit({}), self.h._cmd_toast({"message": "x"}),
                     self.h._cmd_pip_show({"camera": "camera.a", "session": "sess-0001"}), self.h._cmd_power({"action": "suspend"}),
                     self.h._cmd_power({"action": "restart_steam"})):
            with self.assertRaises(m.ProtocolError) as cm:
                await coro
            self.assertEqual(cm.exception.code, m.UNSUPPORTED)
            self.assertIn("game mode", cm.exception.message)
        self.assertEqual(fake_decky.emitted, [])

    async def test_switching_back_to_game_mode_still_works(self):
        with mock.patch.object(Host, "_switch_session") as sw:
            await self.h._cmd_power({"action": "game"})
        sw.assert_called_once_with(False)

    async def test_audio_and_screen_work_in_desktop_mode(self):
        self.h.audio_mode = "system"
        await self.h._cmd_audio({"action": "set_volume", "value": 20})
        self.h._set_app_state(appid="", name="")
        self.assertEqual(self.h.groups["apps"]["screen"], "desktop")


class SaveAppsTests(unittest.IsolatedAsyncioTestCase):
    async def test_saving_apps_updates_the_source_list_in_the_apps_group(self):
        use_temp_settings_dir()
        h = Host()
        h.loop = asyncio.get_running_loop()
        await h.report_app_state("", "")
        self.assertEqual(h.groups["apps"]["apps"], [])
        await h.save_apps([{"appid": "42", "name": "Other", "kind": "steam"}])
        self.assertEqual([a["appid"] for a in h.groups["apps"]["apps"]], ["42"])

    async def test_saving_apps_before_the_frontend_reported_pushes_nothing(self):
        use_temp_settings_dir()
        h = Host()
        await h.save_apps([{"appid": "42", "name": "Other"}])
        self.assertNotIn("apps", h.groups)
