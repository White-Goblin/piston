import asyncio
import unittest
from unittest import mock

from . import StubLifecycle, fake as fake_decky, plugin_environment_patches, use_temp_settings_dir
from piston_device import advertise, resume as resume_module
from piston_device.proto import messages as m
from piston_device.sendspin import crypto, mixin as sendspin_mixin
from piston_device.sendspin.sink import FakeSink
from tests.sendspin_peer import FakeServer
import main


class PluginCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        StubLifecycle.instances.clear()
        use_temp_settings_dir()
        fake_decky.emitted.clear()
        self.plugin = main.Plugin()
        self.pushed = []
        patches = [
            mock.patch.object(main, "DEFAULT_PORT", 0),
            mock.patch("piston_device.advertise.outbound_ip", lambda: None),
            mock.patch("piston_device.state.wol_info", lambda: {"interface": "", "error": "none"}),
            mock.patch("piston_device.audio.AudioMixin._cec_audio_address", lambda self: None),
            mock.patch.object(main.Plugin, "_read_sensors", lambda self: {"cpu_load": 1.0}),
            mock.patch.object(main.Plugin, "_start_http_server", mock.AsyncMock()),
            mock.patch.object(main.Plugin, "_audio_state", lambda self: {"volume": 30, "muted": False}),
            mock.patch.object(sendspin_mixin, "PipeWireSink", lambda **kw: FakeSink()),
        ]
        for p in patches + plugin_environment_patches():
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(self._unload)

    async def _unload(self):
        await self.plugin._unload()

    async def boot(self, **settings):
        if settings:
            self.plugin._update_settings(settings)
        await self.plugin._main()
        real = self.plugin.server.push_state
        self.plugin.server.push_state = lambda name, data: (self.pushed.append((name, data)), real(name, data))[0]

    async def until(self, predicate, timeout=3.0):
        end = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > end:
                raise AssertionError("condition not reached in time")
            await asyncio.sleep(0.01)

    def group(self):
        return self.plugin.groups.get("sendspin")


class StartTests(PluginCase):
    async def test_with_default_settings_the_player_starts_and_the_group_says_waiting(self):
        await self.boot()
        g = self.group()
        self.assertEqual((g["enabled"], g["status"]), (True, "waiting"))
        self.assertGreater(g["port"], 0)
        self.assertEqual(g["name"], self.plugin._load_settings()["device_name"][:64])
        self.assertIsNotNone(self.plugin.sendspin_listener)
        self.assertEqual(self.plugin.sendspin_listener.port, g["port"])

    async def test_the_group_is_valid_and_part_of_the_snapshot(self):
        await self.boot()
        m.sendspin_group(self.group())
        self.assertEqual(self.plugin._snapshot()["sendspin"], self.group())   # a connecting integration gets it first thing

    async def test_disabled_in_the_settings_it_does_not_listen(self):
        await self.boot(sendspin_enabled=False)
        self.assertEqual((self.group()["enabled"], self.group()["status"]), (False, "disabled"))
        self.assertIsNone(self.plugin.sendspin_listener)
        self.assertNotIn("port", self.group())

    async def test_the_identity_key_is_created_and_the_client_id_is_its_public_key(self):
        await self.boot()
        key = self.plugin._sendspin_private_key()
        self.assertEqual(self.plugin._sendspin_client_id, sendspin_mixin.client_id_for(crypto.x25519_public(key)))
        self.assertEqual(self.plugin.sendspin_advertiser.port, self.group()["port"])

    async def test_a_crypto_that_does_not_work_is_an_error_status_and_the_plugin_still_starts(self):
        with mock.patch.object(crypto, "load", side_effect=crypto.CryptoUnavailable("no libcrypto")):
            await self.boot()
        self.assertEqual(self.group()["status"], "error")
        self.assertIn("crypto unavailable", self.group()["error"])
        self.assertIsNone(self.plugin.sendspin_listener)
        self.assertIsNotNone(self.plugin.server)

    async def test_no_free_port_is_an_error_status(self):
        blocker = await asyncio.start_server(lambda r, w: None, "0.0.0.0", 0)
        port = blocker.sockets[0].getsockname()[1]
        self.addCleanup(blocker.close)
        with mock.patch.object(sendspin_mixin, "LISTEN_PORT", port), mock.patch.object(sendspin_mixin, "SendspinListener",
                                                                                      lambda *a, **k: __import__("piston_device.sendspin.listener", fromlist=["x"]).SendspinListener(*a, **{**k, "tries": 1})):
            await self.boot()
        self.assertEqual(self.group()["status"], "error")
        self.assertIn("no free port", self.group()["error"])


class EnableCommandTests(PluginCase):
    async def test_disable_stops_withdraws_persists_and_pushes(self):
        await self.boot()
        port = self.plugin.sendspin_listener.port
        advertiser = self.plugin.sendspin_advertiser
        with mock.patch.object(advertiser, "stop", wraps=advertiser.stop) as stopped:
            self.assertEqual(await self.plugin._cmd_sendspin_enable({"enabled": False}), {})
        stopped.assert_awaited()
        self.assertIsNone(self.plugin.sendspin_listener)
        self.assertEqual(self.group()["status"], "disabled")
        self.assertFalse(self.plugin._load_settings()["sendspin_enabled"])
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)                   # nothing listens any more
        self.assertIn(("sendspin", self.group()), self.pushed)

    async def test_enable_starts_again_and_survives_a_restart_of_the_plugin(self):
        await self.boot(sendspin_enabled=False)
        await self.plugin._cmd_sendspin_enable({"enabled": True})
        self.assertEqual(self.group()["status"], "waiting")
        self.assertTrue(self.plugin._load_settings()["sendspin_enabled"])
        await self.plugin._unload()
        again = main.Plugin()
        self.assertTrue(again._load_settings()["sendspin_enabled"])

    async def test_enabling_twice_is_harmless(self):
        await self.boot()
        listener = self.plugin.sendspin_listener
        await self.plugin._cmd_sendspin_enable({"enabled": True})
        self.assertIs(self.plugin.sendspin_listener, listener)

    async def test_bad_data_is_bad_request(self):
        await self.boot()
        for data in (None, {}, {"enabled": "yes"}, {"enabled": 1}):
            with self.assertRaises(m.ProtocolError) as cm:
                await self.plugin._cmd_sendspin_enable(data)
            self.assertEqual(cm.exception.code, m.BAD_REQUEST)
        self.assertTrue(self.plugin._load_settings()["sendspin_enabled"])

    async def test_the_command_is_registered(self):
        await self.boot()
        self.assertIn("sendspin_enable", self.plugin._commands())

    async def test_a_failing_settings_write_is_an_internal_error(self):
        await self.boot()
        with mock.patch.object(main.Plugin, "_save_sendspin", side_effect=OSError("disk full")):
            with self.assertRaises(m.ProtocolError) as cm:
                await self.plugin._cmd_sendspin_enable({"enabled": False})
        self.assertEqual(cm.exception.code, m.INTERNAL)


class ConnectionTests(PluginCase):
    async def connect_server(self):
        peer = FakeServer()
        await peer.connect(self.plugin.sendspin_listener.port)
        await peer.handshake()
        await peer.hello_and_activate()
        peer.start_pump()
        self.addCleanup(peer.close)
        return peer

    async def test_a_server_connecting_shows_in_the_group_and_leaving_returns_to_waiting(self):
        await self.boot()
        peer = await self.connect_server()
        await self.until(lambda: self.group()["status"] == "connected")
        self.assertEqual(self.group()["server_name"], "Test Server")
        await peer.send("stream/start", {"server_transmitted": 1, "player": {"codec": "pcm", "sample_rate": 48000, "channels": 2, "bit_depth": 16}})
        await self.until(lambda: self.group()["status"] == "streaming")
        peer.pump.cancel()
        peer.ws.close()
        await self.until(lambda: self.group()["status"] == "waiting")
        self.assertNotIn("server_name", self.group())

    async def test_disabling_while_connected_says_goodbye_to_the_server(self):
        await self.boot()
        peer = await self.connect_server()
        await self.until(lambda: self.group()["status"] == "connected")
        await self.plugin._cmd_sendspin_enable({"enabled": False})
        self.assertEqual(await peer.next("client/goodbye"), ("client/goodbye", {"reason": "user_request"}))

    async def test_unloading_the_plugin_says_goodbye_and_releases_the_port(self):
        await self.boot()
        peer = await self.connect_server()
        await self.until(lambda: self.group()["status"] == "connected")
        await self.plugin._unload()
        self.assertEqual(await peer.next("client/goodbye"), ("client/goodbye", {"reason": "shutdown"}))
        self.assertIsNone(self.plugin.sendspin_listener)

    async def test_the_level_set_by_the_server_is_saved_once_it_settles(self):
        await self.boot()
        peer = await self.connect_server()
        await self.until(lambda: self.group()["status"] == "connected")
        with mock.patch.object(sendspin_mixin, "SETTINGS_DEBOUNCE_S", 0.1):
            await peer.send("server/command", {"player": {"command": "volume", "volume": 37}})   # an announcement boost
            await peer.send("server/command", {"player": {"command": "volume", "volume": 20}})   # restored
            await peer.send("server/command", {"player": {"command": "mute", "mute": True}})
            await self.until(lambda: self.plugin._load_settings()["sendspin_muted"])
        s = self.plugin._load_settings()
        self.assertEqual((s["sendspin_volume"], s["sendspin_muted"]), (20, True))

    async def test_pending_level_changes_are_saved_at_unload(self):
        await self.boot()
        self.plugin._sendspin_settings_changed(volume=12)
        self.assertEqual(self.plugin._load_settings()["sendspin_volume"], 50)
        await self.plugin._unload()
        self.assertEqual(self.plugin._load_settings()["sendspin_volume"], 12)

    async def test_each_session_gets_its_own_sink_with_the_saved_level(self):
        await self.boot(sendspin_volume=33, sendspin_muted=True)
        made = []
        with mock.patch.object(sendspin_mixin, "PipeWireSink", lambda **kw: made.append(kw) or FakeSink()):
            await self.connect_server()
            await self.until(lambda: made)
        self.assertEqual(made, [{"volume": 33, "muted": True}])


class AdvertisingTests(PluginCase):
    async def test_the_sendspin_record_is_advertised_on_the_listener_port_when_there_is_a_network(self):
        created = []

        class Low:
            def __init__(self, stype, instance, host, port, ip, txt):
                self.args = (stype, instance, host, port, ip, txt)
                created.append(self)

            async def start(self): pass
            async def stop(self): self.stopped = True
            async def refresh(self, **txt): pass
        with mock.patch("piston_device.advertise.outbound_ip", lambda: "10.0.0.2"), mock.patch.object(advertise, "MdnsAdvertiser", Low), \
                mock.patch.object(sendspin_mixin, "sendspin_advertiser", lambda info, port: advertise.sendspin_advertiser(info, port, factory=Low)):
            await self.boot()
            await asyncio.sleep(0.05)
        low = next(c for c in created if c.args[0] == "_sendspin._tcp.local.")
        self.assertEqual(low.args[3], self.group()["port"])
        self.assertEqual(low.args[1], self.plugin._sendspin_client_id)
        self.assertEqual(low.args[5], {"path": "/sendspin", "name": self.group()["name"]})
        await self.plugin._sendspin_stop()
        self.assertTrue(low.stopped)


class SleepTests(PluginCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.network = mock.AsyncMock(return_value=True)
        for p in (mock.patch.object(resume_module, "wait_for_network", self.network), mock.patch.object(resume_module, "APP_SYNC_FIRST_WAIT", 0.02),
                  mock.patch.object(resume_module, "APP_SYNC_WINDOW", 0.1), mock.patch.object(resume_module, "APP_SYNC_RETRY", 0.02)):
            p.start()
            self.addCleanup(p.stop)
        self.events = []

    async def boot(self, **settings):
        await super().boot(**settings)
        real = self.plugin.server.push_event
        self.plugin.server.push_event = lambda name, data=None: (self.events.append(name), real(name, data))[0]

    async def connect_server(self):
        peer = FakeServer()
        await peer.connect(self.plugin.sendspin_listener.port)
        await peer.handshake()
        await peer.hello_and_activate()
        peer.start_pump()
        self.addCleanup(peer.close)
        await self.until(lambda: self.group()["status"] == "connected")
        return peer

    async def wake(self):
        await self.plugin._announce_resume()
        await self.plugin._sendspin_wake_task

    async def test_the_sleep_announcement_stops_the_player_and_says_goodbye(self):
        await self.boot()
        peer = await self.connect_server()
        port = self.plugin.sendspin_listener.port
        advertiser = self.plugin.sendspin_advertiser
        with mock.patch.object(advertiser, "stop", wraps=advertiser.stop) as withdrawn:
            await self.plugin._announce_sleep()
        withdrawn.assert_awaited()
        self.assertEqual(await peer.next("client/goodbye"), ("client/goodbye", {"reason": "shutdown"}))
        self.assertIsNone(self.plugin.sendspin_listener)
        self.assertEqual(self.events, ["going_to_sleep"])
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)

    async def test_the_shutdown_announcement_passes_the_reason_on(self):
        await self.boot()
        peer = await self.connect_server()
        await self.plugin._announce_shutdown("restart")
        self.assertEqual(await peer.next("client/goodbye"), ("client/goodbye", {"reason": "restart"}))
        self.assertEqual(self.events, ["shutting_down"])

    async def test_a_player_that_cannot_stop_in_time_does_not_hold_up_the_sleep(self):
        await self.boot()

        async def hang(*a, **k):
            await asyncio.sleep(30)
        with mock.patch.object(sendspin_mixin, "SLEEP_STOP_TIMEOUT_S", 0.05), mock.patch.object(main.Plugin, "_sendspin_stop", hang):
            await asyncio.wait_for(self.plugin._announce_sleep(), 2)
        self.assertEqual(self.events, ["going_to_sleep"])

    async def test_a_player_that_fails_to_stop_does_not_break_the_sleep(self):
        await self.boot()
        with mock.patch.object(main.Plugin, "_sendspin_stop", mock.AsyncMock(side_effect=RuntimeError("boom"))):
            await self.plugin._announce_sleep()
        self.assertEqual(self.events, ["going_to_sleep"])

    async def test_resume_starts_the_player_again_and_a_server_can_connect(self):
        await self.boot()
        await self.plugin._announce_sleep()
        self.assertIsNone(self.plugin.sendspin_listener)
        await self.wake()
        self.assertIsNotNone(self.plugin.sendspin_listener)
        self.assertEqual(self.group()["status"], "waiting")
        self.assertEqual(self.events, ["going_to_sleep", "resumed"])
        await self.connect_server()
        self.network.assert_awaited()

    async def test_resume_without_a_network_still_starts_the_player(self):
        await self.boot()
        await self.plugin._announce_sleep()
        self.network.return_value = False
        await self.wake()
        self.assertIsNotNone(self.plugin.sendspin_listener)         # the advertiser keeps trying until the address is there

    async def test_the_resumed_event_does_not_wait_for_the_player(self):
        await self.boot()
        await self.plugin._announce_sleep()
        gate = asyncio.Event()
        real = main.Plugin._sendspin_start

        async def slow(self_):
            await gate.wait()
            return await real(self_)
        with mock.patch.object(main.Plugin, "_sendspin_start", slow):
            await self.plugin._announce_resume()
            self.assertIn("resumed", self.events)
            self.assertIsNone(self.plugin.sendspin_listener)
            gate.set()
            await self.plugin._sendspin_wake_task
        self.assertIsNotNone(self.plugin.sendspin_listener)

    async def test_a_suspend_nobody_announced_still_ends_with_a_fresh_player(self):
        await self.boot()
        peer = await self.connect_server()
        old = self.plugin.sendspin_listener
        await self.wake()                                              # resume with no sleep announcement before it
        self.assertEqual(await peer.next("client/goodbye"), ("client/goodbye", {"reason": "restart"}))
        self.assertIsNotNone(self.plugin.sendspin_listener)
        self.assertIsNot(self.plugin.sendspin_listener, old)

    async def test_the_player_cannot_be_started_while_the_device_is_going_to_sleep(self):
        await self.boot()
        await self.plugin._announce_sleep()
        await self.plugin._cmd_sendspin_enable({"enabled": True})
        self.assertIsNone(self.plugin.sendspin_listener)
        await self.wake()
        self.assertIsNotNone(self.plugin.sendspin_listener)

    async def test_a_disabled_player_stays_down_through_sleep_and_resume(self):
        await self.boot(sendspin_enabled=False)
        await self.plugin._announce_sleep()
        await self.wake()
        self.assertIsNone(self.plugin.sendspin_listener)
        self.assertEqual(self.group()["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
