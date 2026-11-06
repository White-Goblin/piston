import asyncio
import unittest
from unittest import mock

from . import StubLifecycle, fake as fake_decky, plugin_environment_patches, use_temp_settings_dir, wsclient
from piston_device import server as server_module
from piston_device.proto import PROTOCOL_VERSION, auth, messages as m, pake
import main


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        StubLifecycle.instances.clear()
        use_temp_settings_dir()
        fake_decky.emitted.clear()
        self.plugin = main.Plugin()
        patches = [
            mock.patch.object(main, "DEFAULT_PORT", 0),
            mock.patch("piston_device.advertise.outbound_ip", lambda: None),
            mock.patch("piston_device.state.wol_info", lambda: {"interface": "", "error": "none"}),
            mock.patch("piston_device.audio.AudioMixin._cec_audio_address", lambda self: None),
            mock.patch.object(main.Plugin, "_read_sensors", lambda self: {"cpu_load": 1.0}),
            mock.patch.object(main.Plugin, "_start_http_server", mock.AsyncMock()),
            mock.patch.object(main.Plugin, "_audio_state", lambda self: {"volume": 30, "muted": False}),
        ]
        for p in patches + plugin_environment_patches():
            p.start()
            self.addCleanup(p.stop)
        await self.plugin._main()
        self.addAsyncCleanup(self.plugin._unload)

    async def test_lifecycle_is_started_with_the_plugin_and_stopped_on_unload(self):
        (lifecycle,) = StubLifecycle.instances
        self.assertTrue(lifecycle.started)
        self.assertIs(self.plugin.lifecycle, lifecycle)
        self.assertFalse(lifecycle.stopped)
        await self.plugin._unload()                # synchronous inside: the loop may be starved (see Plugin._unload_now)
        await self.plugin._unload()                # the cleanup unloads again later; that must stay harmless
        self.assertTrue(lifecycle.aborted)

    async def test_lifecycle_callbacks_send_the_events_to_the_integration(self):
        (lifecycle,) = StubLifecycle.instances
        sent, flushed = [], []
        self.plugin.server.push_event = lambda name, data=None: sent.append((name, data))
        self.plugin.server.flush = mock.AsyncMock(side_effect=lambda: flushed.append(1))
        await lifecycle.callbacks["on_sleep"]()
        await lifecycle.callbacks["on_shutdown"]("restart")
        self.assertEqual(sent, [("going_to_sleep", {"reason": "suspend"}), ("shutting_down", {"reason": "restart"})])
        self.assertEqual(len(flushed), 2)                      # each was flushed before returning

    async def test_remote_input_is_wired_to_the_server_the_link_and_the_unload(self):
        self.assertEqual(self.plugin.server.on_input, self.plugin._on_input)
        self.assertTrue((await self.plugin.get_status())["remote_input"])
        self.assertEqual((await self.plugin._device_group())["input"], {"enabled": True})
        calls = []
        self.plugin._input_release = lambda: calls.append("release")
        self.plugin._on_link(False)
        self.assertEqual(calls, ["release"])                     # the link went away: let go of everything
        self.plugin._on_link(True)
        self.assertEqual(calls, ["release"])
        self.plugin._input_abort = lambda: calls.append("abort")
        self.plugin._unload_now()
        self.assertIn("abort", calls)                            # unload closes the virtual devices, synchronously

    async def test_the_remote_input_choice_shows_in_the_status_and_survives_a_restart(self):
        status = await self.plugin.get_status()
        self.assertEqual((status["remote_input"], status["remote_input_error"]), (True, ""))
        await self.plugin.set_remote_input(False)
        self.assertFalse((await self.plugin.get_status())["remote_input"])
        self.assertFalse((await self.plugin.get_settings())["remote_input"])
        restarted = main.Plugin()
        restarted._init_input()                                  # what _main does after a restart: read the saved choice
        self.assertFalse(restarted.remote_input)
        restarted.input_keys.shutdown(wait=False)
        self.plugin.input_error = "no uinput"
        await self.plugin.set_remote_input(True)
        self.plugin.input_error = "no uinput"
        self.assertEqual((await self.plugin.get_status())["remote_input_error"], "no uinput")
        await self.plugin.set_remote_input(False)
        self.assertEqual((await self.plugin.get_status())["remote_input_error"], "")   # an error is only shown while input is on

    async def test_starts_unpaired_with_a_pairing_code(self):
        status = await self.plugin.get_status()
        self.assertFalse(status["paired"])
        self.assertEqual(len(status["device_id"]), 12)
        state = await self.plugin.get_pairing_state()
        self.assertEqual(len(state["code"]), 6)

    async def test_pair_over_websocket_then_command_and_state(self):
        port = self.plugin.server.port
        hello = {"type": "hello", "proto": PROTOCOL_VERSION, "min_proto": 1, "device_id": "ha"}
        c = await wsclient.WsTestClient.connect(port)
        first = await c.recv()
        self.assertFalse(first["paired"])
        await c.send(hello)
        await c.send({"type": "pair", "step": "begin"})
        ch = await c.recv()
        client = pake.SrpClient((await self.plugin.get_pairing_state())["code"])
        m1 = client.process(bytes.fromhex(ch["salt"]), pake.from_hex(ch["B"]))
        await c.send({"type": "pair", "step": "proof", "A": pake.to_hex(client.A), "M1": m1.hex()})
        client.confirm(bytes.fromhex((await c.recv())["M2"]))
        names = set()
        while len(names) < 4:
            frame = await c.recv()
            if frame["type"] == "state":
                names.add(frame["name"])
        self.assertTrue({"audio", "pip", "controllers", "device"} <= names | {"device"})
        self.assertTrue((await self.plugin.get_status())["paired"])
        await c.send({"type": "cmd", "id": 1, "name": "toast", "data": {"message": "hello"}})
        while True:
            frame = await c.recv()
            if frame["type"] == "result":
                break
        self.assertTrue(frame["ok"])
        self.assertEqual(fake_decky.emitted[-1][0], "notification")

    async def test_unpair_command_returns_to_unpaired_and_drops_the_connection(self):
        self.plugin._update_settings({"secret": "ab" * 32})
        secret = bytes.fromhex("ab" * 32)
        c = await wsclient.WsTestClient.connect(self.plugin.server.port)
        await c.recv()
        await c.send({"type": "hello", "proto": PROTOCOL_VERSION, "min_proto": 1, "device_id": "ha"})
        challenge = await c.recv()
        nh = auth.make_challenge()
        await c.send({"type": "proof", "nonce": nh, "proof": auth.prove(secret, challenge["nonce"], nh, auth.ROLE_HA)})
        await c.recv()
        await c.send({"type": "cmd", "id": 5, "name": "unpair"})
        while True:
            frame = await c.recv()
            if frame.get("type") == "result":
                break
        self.assertTrue(frame["ok"])
        self.assertFalse((await self.plugin.get_status())["paired"])
        self.assertEqual(len((await self.plugin.get_pairing_state())["code"]), 6)

    async def test_reset_pairing_callable(self):
        self.plugin._update_settings({"secret": "cd" * 32})
        state = await self.plugin.reset_pairing()
        self.assertFalse(state["paired"])
        self.assertEqual(self.plugin._load_settings()["secret"], "")


class UnloadWhileStartingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unload_before_main_finished_does_not_fail(self):
        """Decky can unload a plugin that has only just been constructed (an install right after a start)."""
        import main
        await main.Plugin()._unload()
