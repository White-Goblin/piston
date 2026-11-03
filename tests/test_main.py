import asyncio
import unittest
from unittest import mock

from . import fake as fake_decky, use_temp_settings_dir, wsclient
from decky_ha import server as server_module
from decky_ha.proto import PROTOCOL_VERSION, auth, messages as m, pake
import main


class StubLifecycle:
    """Stands in for decky_ha.lifecycle.Lifecycle so tests never start gdbus or take a real inhibitor."""
    instances = []

    def __init__(self, **callbacks):
        self.callbacks, self.active, self.protecting, self.started, self.stopped = callbacks, False, False, False, False
        StubLifecycle.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        StubLifecycle.instances.clear()
        use_temp_settings_dir()
        fake_decky.emitted.clear()
        self.plugin = main.Plugin()
        patches = [
            mock.patch.object(main, "DEFAULT_PORT", 0),
            mock.patch("decky_ha.state.Lifecycle", StubLifecycle),
            mock.patch("decky_ha.advertise.outbound_ip", lambda: None),
            mock.patch("decky_ha.state.wol_info", lambda: {"interface": "", "error": "none"}),
            mock.patch("decky_ha.audio.AudioMixin._cec_audio_address", lambda self: None),
            mock.patch.object(main.Plugin, "_read_sensors", lambda self: {"cpu_load": 1.0}),
            mock.patch.object(main.Plugin, "_start_http_server", mock.AsyncMock()),
            mock.patch.object(main.Plugin, "_audio_state", lambda self: {"volume": 30, "muted": False}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        await self.plugin._main()
        self.addAsyncCleanup(self.plugin._unload)

    async def test_lifecycle_is_started_with_the_plugin_and_stopped_on_unload(self):
        (lifecycle,) = StubLifecycle.instances
        self.assertTrue(lifecycle.started)
        self.assertIs(self.plugin.lifecycle, lifecycle)
        self.assertFalse(lifecycle.stopped)
        with mock.patch.object(self.plugin, "_stop_lifecycle", wraps=self.plugin._stop_lifecycle) as stop:
            await self.plugin._unload()
            await self.plugin._unload()            # the cleanup unloads again later; that must stay harmless
        self.assertEqual(stop.await_count, 2)
        self.assertTrue(lifecycle.stopped)

    async def test_lifecycle_callbacks_send_the_events_to_the_integration(self):
        (lifecycle,) = StubLifecycle.instances
        sent, flushed = [], []
        self.plugin.server.push_event = lambda name, data=None: sent.append((name, data))
        self.plugin.server.flush = mock.AsyncMock(side_effect=lambda: flushed.append(1))
        await lifecycle.callbacks["on_sleep"]()
        await lifecycle.callbacks["on_shutdown"]("restart")
        self.assertEqual(sent, [("going_to_sleep", {"reason": "suspend"}), ("shutting_down", {"reason": "restart"})])
        self.assertEqual(len(flushed), 2)                      # each was flushed before returning

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
