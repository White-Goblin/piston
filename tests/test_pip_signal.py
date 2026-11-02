import asyncio
import json
import unittest

from . import fake as fake_decky, use_temp_settings_dir
from decky_ha.control import ControlMixin
from decky_ha.http_server import HttpServerMixin
from decky_ha.pip import PipMixin
from decky_ha.pip_signal import MAX_QUEUE, PipSignalMixin
from decky_ha.proto import messages as m
from decky_ha.settings import SettingsMixin
from decky_ha.state import StateMixin

SESSION = "sess-abc12345"


class Host(SettingsMixin, StateMixin, ControlMixin, PipMixin, PipSignalMixin, HttpServerMixin):
    def __init__(self):
        self._init_state()
        self._init_pip_signal()
        self.events = []
        self.nonce = "N0NCE"
        self.http_server = None

    def _push_event(self, name, data=None):
        self.events.append((name, data))


class SignalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        use_temp_settings_dir()
        fake_decky.emitted.clear()
        self.h = Host()
        self.h._pip_open(SESSION)

    async def test_bad_session_ids_are_refused(self):
        for bad in ("", "short", "has space 123", "x" * 65, "../etc/passwd"):
            with self.assertRaises(m.ProtocolError):
                self.h._pip_open(bad)

    async def test_command_messages_reach_the_poller_in_order(self):
        await self.h._cmd_pip_signal({"session": SESSION, "kind": "config", "webrtc": True})
        await self.h._cmd_pip_signal({"session": SESSION, "kind": "answer", "answer": "sdp"})
        got = await self.h.pip_poll(SESSION, 0, timeout=1)
        self.assertEqual([x["kind"] for x in got["messages"]], ["config", "answer"])
        self.assertEqual((got["next"], got["closed"]), (2, False))
        await self.h._cmd_pip_signal({"session": SESSION, "kind": "candidate", "candidate": {}})
        again = await self.h.pip_poll(SESSION, got["next"], timeout=1)
        self.assertEqual([x["kind"] for x in again["messages"]], ["candidate"])

    async def test_poll_waits_for_a_message(self):
        task = asyncio.create_task(self.h.pip_poll(SESSION, 0, timeout=5))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        await self.h._cmd_pip_signal({"session": SESSION, "kind": "error", "message": "x"})
        self.assertEqual((await task)["messages"][0]["kind"], "error")

    async def test_poll_times_out_with_nothing(self):
        got = await self.h.pip_poll(SESSION, 0, timeout=0.05)
        self.assertEqual((got["messages"], got["closed"]), ([], False))

    async def test_unknown_session_is_closed_for_the_poller_and_refused_for_the_integration(self):
        self.assertTrue((await self.h.pip_poll("nope-12345", 0, timeout=1))["closed"])
        with self.assertRaises(m.ProtocolError):
            await self.h._cmd_pip_signal({"session": "nope-12345", "kind": "answer"})

    async def test_player_messages_become_events_and_close_ends_the_session(self):
        self.assertTrue(self.h.pip_send(SESSION, {"kind": "start", "camera": "camera.a"}))
        self.assertEqual(self.h.events[-1], ("pip_signal", {"session": SESSION, "kind": "start", "camera": "camera.a"}))
        with self.assertRaises(ValueError):
            self.h.pip_send(SESSION, {"kind": "answer"})
        self.assertTrue(self.h.pip_send(SESSION, {"kind": "close"}))
        self.assertNotIn(SESSION, self.h.pip_sessions)
        self.assertFalse(self.h.pip_send(SESSION, {"kind": "start"}))

    async def test_new_show_closes_the_previous_session_and_tells_the_integration(self):
        await self.h._cmd_pip_show({"camera": "camera.b", "session": "second-sess-1"})
        self.assertEqual(list(self.h.pip_sessions), ["second-sess-1"])
        self.assertEqual(self.h.events[-1], ("pip_signal", {"session": SESSION, "kind": "close"}))

    async def test_hide_closes_all_sessions_quietly(self):
        before = len(self.h.events)
        await self.h._cmd_pip_hide({})
        self.assertEqual((self.h.pip_sessions, len(self.h.events)), ({}, before))

    async def test_queue_is_bounded(self):
        for _ in range(MAX_QUEUE):
            await self.h._cmd_pip_signal({"session": SESSION, "kind": "candidate"})
        with self.assertRaises(m.ProtocolError) as cm:
            await self.h._cmd_pip_signal({"session": SESSION, "kind": "candidate"})
        self.assertEqual(cm.exception.code, m.BUSY)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        use_temp_settings_dir()
        self.h = Host()
        self.h._pip_open(SESSION)
        self.server = await asyncio.start_server(self.h._serve_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(self._close)

    async def _close(self):
        self.server.close()

    async def request(self, method, path, body=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        payload = json.dumps(body).encode() if body is not None else b""
        writer.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload)
        await writer.drain()
        raw = await reader.read()
        writer.close()
        head, _, rest = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), (json.loads(rest) if rest.startswith(b"{") else rest)

    async def test_send_forwards_to_the_integration(self):
        status, body = await self.request("POST", f"/pip/send?nonce=N0NCE&session={SESSION}", {"kind": "start", "camera": "camera.a"})
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertEqual(self.h.events[-1][1]["kind"], "start")

    async def test_events_returns_queued_messages(self):
        await self.h._cmd_pip_signal({"session": SESSION, "kind": "config", "webrtc": False})
        status, body = await self.request("GET", f"/pip/events?nonce=N0NCE&session={SESSION}&after=0")
        self.assertEqual((status, body["messages"][0]["kind"], body["next"]), (200, "config", 1))

    async def test_wrong_nonce_is_forbidden(self):
        for method, path in (("GET", f"/pip/events?nonce=bad&session={SESSION}"), ("POST", f"/pip/send?session={SESSION}")):
            status, _ = await self.request(method, path, {"kind": "start"} if method == "POST" else None)
            self.assertEqual(status, 403)

    async def test_bad_input(self):
        self.assertEqual((await self.request("GET", f"/pip/events?nonce=N0NCE&session={SESSION}&after=abc"))[0], 400)
        self.assertEqual((await self.request("POST", f"/pip/send?nonce=N0NCE&session={SESSION}", {"kind": "answer"}))[0], 400)
        self.assertEqual((await self.request("POST", f"/pip/send?nonce=N0NCE&session=unknown-1234", {"kind": "start"}))[0], 404)
        self.assertEqual((await self.request("DELETE", "/pip/send"))[0], 405)
        self.assertEqual((await self.request("GET", "/nothing"))[0], 404)

    async def test_oversize_body_is_refused(self):
        status, _ = await self.request("POST", f"/pip/send?nonce=N0NCE&session={SESSION}", {"kind": "start", "x": "a" * 70000})
        self.assertEqual(status, 413)

    async def test_no_token_route_any_more(self):
        self.assertEqual((await self.request("GET", "/token?nonce=N0NCE"))[0], 404)
