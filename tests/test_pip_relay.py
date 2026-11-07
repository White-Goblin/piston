import asyncio
import unittest

from custom_components.piston.client import CommandError
from custom_components.piston.pip import PipRelay
from .entityutil import connect, make_coordinator


class FakeMessage:
    def __init__(self, body):
        self.body = body

    def as_dict(self):
        return self.body


class FakeStream:
    def __init__(self):
        self.providers, self.started = [], False

    def add_provider(self, fmt):
        self.providers.append(fmt)

    async def start(self):
        self.started = True

    def endpoint_url(self, fmt):
        return "/api/hls/tok/master_playlist.m3u8"


class FakeCamera:
    def __init__(self, webrtc=True, offer_error=None, no_stream=False):
        self.camera_capabilities = type("C", (), {"frontend_stream_types": {"hls", "web_rtc"} if webrtc else {"hls"}})()
        self.offers, self.candidates, self.closed = [], [], []
        self.offer_error, self.no_stream, self.stream = offer_error, no_stream, FakeStream()
        self.candidate_failures = 0

    def async_get_webrtc_client_configuration(self):
        return type("Cfg", (), {"to_frontend_dict": lambda s: {"configuration": {"iceServers": [{"urls": ["stun:x"]}]}}})()

    async def async_handle_async_webrtc_offer(self, sdp, session, send):
        self.offers.append((sdp, session))
        if self.offer_error:
            raise self.offer_error
        send(FakeMessage({"type": "session", "session_id": session}))
        send(FakeMessage({"type": "answer", "answer": "ANSWER-SDP"}))
        send(FakeMessage({"type": "candidate", "candidate": {"candidate": "c1"}}))

    async def async_on_webrtc_candidate(self, session, candidate):
        if self.candidate_failures:
            self.candidate_failures -= 1
            raise RuntimeError("unknown session")
        self.candidates.append((session, candidate))

    def close_webrtc_session(self, session):
        self.closed.append(session)

    async def async_create_stream(self):
        return None if self.no_stream else self.stream


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coord = make_coordinator()
        connect(self.coord)
        self.cam = FakeCamera()
        self.relay = PipRelay(self.coord.hass, self.coord, get_camera=lambda hass, eid: self.cam,
                              parse_candidate=lambda d: ("parsed", d), base_url=lambda hass: "http://ha.local:8123")
        self.relay.start()
        self.addCleanup(self.relay.stop)
        self.session = await self.relay.async_show("camera.door", {"position": "top-right", "duration": 30000})

    def signals(self):
        return [c[1] for c in self.coord.client.calls if c[0] == "pip_signal"]

    async def signal(self, **data):
        self.coord.on_event("pip_signal", {"session": self.session, **data})
        await self.relay.async_wait_idle()

    async def test_show_asks_the_device_with_a_fresh_session_and_the_options(self):
        name, data = self.coord.client.calls[0]
        self.assertEqual(name, "pip_show")
        self.assertEqual((data["camera"], data["position"], data["duration"]), ("camera.door", "top-right", 30000))
        self.assertRegex(data["session"], r"^[A-Za-z0-9_-]{8,64}$")
        self.assertEqual(self.relay.sessions, {data["session"]: "camera.door"})

    async def test_start_replies_with_the_webrtc_config(self):
        await self.signal(kind="start", camera="camera.door")
        self.assertEqual(self.signals()[0], {"session": self.session, "kind": "config", "webrtc": True,
                                             "config": {"configuration": {"iceServers": [{"urls": ["stun:x"]}]}}})

    async def test_camera_without_webrtc_gets_webrtc_false(self):
        self.cam = FakeCamera(webrtc=False)
        await self.signal(kind="start")
        self.assertEqual((self.signals()[0]["webrtc"], "config" in self.signals()[0]), (False, False))

    async def test_offer_returns_answer_and_candidates_and_skips_the_session_message(self):
        await self.signal(kind="offer", sdp="OFFER-SDP")
        self.assertEqual(self.cam.offers, [("OFFER-SDP", self.session)])
        kinds = [s["kind"] for s in self.signals()]
        self.assertEqual(kinds, ["answer", "candidate"])
        self.assertEqual(self.signals()[0]["answer"], "ANSWER-SDP")

    async def test_candidates_are_passed_to_the_camera_and_retried_while_the_offer_starts(self):
        self.cam.candidate_failures = 2
        await self.signal(kind="candidate", candidate={"candidate": "x"})
        self.assertEqual(self.cam.candidates, [(self.session, ("parsed", {"candidate": "x"}))])

    async def test_webrtc_failure_is_reported_so_the_player_can_fall_back(self):
        self.cam.offer_error = RuntimeError("go2rtc_webrtc_offer_failed: DESCRIBE failed: 404")
        await self.signal(kind="offer", sdp="o")
        err = self.signals()[0]
        self.assertEqual((err["kind"], err["code"]), ("error", "pip_offer_failed"))
        self.assertIn("404", err["message"])

    async def test_hls_returns_an_absolute_url(self):
        await self.signal(kind="hls")
        self.assertEqual(self.signals()[0], {"session": self.session, "kind": "hls_url",
                                             "url": "http://ha.local:8123/api/hls/tok/master_playlist.m3u8"})
        self.assertEqual((self.cam.stream.providers, self.cam.stream.started), (["hls"], True))

    async def test_hls_without_a_stream_is_an_error(self):
        self.cam.no_stream = True
        await self.signal(kind="hls")
        self.assertEqual(self.signals()[0]["kind"], "error")

    async def test_close_ends_the_webrtc_session(self):
        await self.signal(kind="close")
        self.assertEqual((self.cam.closed, self.relay.sessions), ([self.session], {}))

    async def test_messages_for_unknown_sessions_are_ignored(self):
        self.coord.on_event("pip_signal", {"session": "other-session", "kind": "start"})
        await self.relay.async_wait_idle()
        self.assertEqual(self.signals(), [])

    async def test_a_new_show_drops_the_old_session(self):
        second = await self.relay.async_show("camera.yard", {})
        self.assertEqual(self.relay.sessions, {second: "camera.yard"})
        self.assertEqual(self.cam.closed, [self.session])

    async def test_hide_closes_everything(self):
        await self.relay.async_hide()
        self.assertEqual((self.relay.sessions, self.coord.client.calls[-1][0]), ({}, "pip_hide"))

    async def test_losing_the_device_closes_sessions(self):
        self.coord.on_connection(False)
        self.assertEqual((self.relay.sessions, self.cam.closed), ({}, [self.session]))

    async def test_a_device_that_forgot_the_session_ends_it_here(self):
        self.coord.client.error = CommandError("bad_request", "unknown or closed PiP session")
        await self.signal(kind="start")
        self.assertEqual(self.relay.sessions, {})


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    """The real plugin backend, the real protocol and client, a fake camera: player -> HA -> camera and back."""

    async def asyncSetUp(self):
        from unittest import mock

        import main
        from custom_components.piston.client import DeckyClient
        from . import plugin_environment_patches, use_temp_settings_dir
        from .stdlib_transport import StdTransport

        use_temp_settings_dir()
        self.plugin = main.Plugin()
        for p in (mock.patch.object(main, "DEFAULT_PORT", 0), mock.patch("piston_device.advertise.outbound_ip", lambda: None),
                  mock.patch("piston_device.state.wol_info", lambda: {"interface": ""}),
                  mock.patch("piston_device.audio.AudioMixin._cec_audio_address", lambda self: None),
                  mock.patch.object(main.Plugin, "_read_sensors", lambda self: {}),
                  mock.patch.object(main.Plugin, "_start_http_server", mock.AsyncMock()),
                  mock.patch.object(main.Plugin, "_audio_state", lambda self: {"volume": 1, "muted": False}),
                  *plugin_environment_patches()):
            p.start()
            self.addCleanup(p.stop)
        self.secret = b"p" * 32
        self.plugin._update_settings({"secret": self.secret.hex()})
        await self.plugin._main()
        self.addAsyncCleanup(self.plugin._unload)
        self.coord = make_coordinator()
        self.up = asyncio.Event()
        client = DeckyClient(StdTransport(), "127.0.0.1", self.plugin.server.port, self.plugin._load_settings()["device_id"],
                             self.secret, on_state=self.coord.on_state, on_event=self.coord.on_event,
                             on_connection=lambda up: (self.coord.on_connection(up), self.up.set() if up else None))
        self.coord.client = client
        await client.start()
        self.addAsyncCleanup(client.stop)
        await asyncio.wait_for(self.up.wait(), 3)
        self.cam = FakeCamera()
        self.relay = PipRelay(self.coord.hass, self.coord, get_camera=lambda h, e: self.cam, parse_candidate=lambda d: d,
                              base_url=lambda h: "http://ha")
        self.relay.start()
        self.addCleanup(self.relay.stop)

    async def poll_kinds(self, session, after=0):
        got = await self.plugin.pip_poll(session, after, timeout=3)
        return [x["kind"] for x in got["messages"]], got["next"], got["messages"]

    async def test_webrtc_round_trip(self):
        session = await self.relay.async_show("camera.door", {})
        self.assertIn(session, self.plugin.pip_sessions)
        self.plugin.pip_send(session, {"kind": "start", "camera": "camera.door"})
        kinds, nxt, msgs = await self.poll_kinds(session)
        self.assertEqual((kinds, msgs[0]["webrtc"]), (["config"], True))
        self.plugin.pip_send(session, {"kind": "offer", "sdp": "O"})
        kinds = []
        while "answer" not in kinds:
            more, nxt, _ = await self.poll_kinds(session, nxt)
            kinds += more
        self.assertEqual(self.cam.offers, [("O", session)])

    async def test_fallback_to_hls_when_webrtc_fails(self):
        self.cam.offer_error = RuntimeError("DESCRIBE failed: 404")
        session = await self.relay.async_show("camera.drive", {})
        self.plugin.pip_send(session, {"kind": "offer", "sdp": "O"})
        kinds, nxt, msgs = await self.poll_kinds(session)
        self.assertEqual(kinds, ["error"])
        self.plugin.pip_send(session, {"kind": "hls"})
        kinds, _, msgs = await self.poll_kinds(session, nxt)
        self.assertEqual((kinds, msgs[0]["url"]), (["hls_url"], "http://ha/api/hls/tok/master_playlist.m3u8"))

    async def test_closing_in_the_player_closes_the_camera_session(self):
        session = await self.relay.async_show("camera.door", {})
        self.plugin.pip_send(session, {"kind": "close"})
        for _ in range(50):
            if self.cam.closed:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.cam.closed, [session])
