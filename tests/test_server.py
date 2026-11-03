import asyncio
import unittest

from . import wsclient
from decky_ha.proto import PROTOCOL_VERSION, auth, messages as m
from decky_ha.server import ProtocolServer

SECRET = b"k" * 32
HELLO = {"type": "hello", "proto": PROTOCOL_VERSION, "min_proto": 1, "device_id": "ha"}


class FakePairing:
    """Pairs on a 'pair' frame carrying code 'ok'; the real state machine is tested separately."""

    def __init__(self, box):
        self.box = box

    async def handle(self, frame):
        if frame.get("code") != "ok":
            raise m.ProtocolError(m.UNAUTHORIZED, "wrong code", {"attempts_left": 3})
        self.box["secret"] = SECRET
        return {"type": "pair", "step": "done"}


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.box = {"secret": SECRET}
        self.log = []
        self.release = asyncio.Event()

        async def ok(data):
            return {"echo": data}

        async def bad(data):
            raise m.ProtocolError(m.NOT_RUNNING, "nothing is running")

        async def boom(data):
            raise RuntimeError("kaput")

        async def slow(data):
            await self.release.wait()
            return {}

        async def launch(data):
            self.log.append(data["appid"])
            return {}

        self.events = []
        self.server = ProtocolServer(
            device_id=lambda: "dev123", secret=lambda: self.box["secret"], pairing=FakePairing(self.box),
            commands={"ok": ok, "bad": bad, "boom": boom, "slow": slow, "launch": launch},
            snapshot=lambda: {"device": {"name": "x"}, "apps": {"running": False}},
            host="127.0.0.1", port=0, ping_interval=0.05, idle_timeout=1, hello_timeout=0.5,
            on_connection=self.events.append)
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)

    async def connect(self, hello=HELLO):
        c = await wsclient.WsTestClient.connect(self.server.port)
        self.addCleanup(c.close)
        first = await c.recv()
        self.assertEqual(first["type"], "hello")
        self.assertEqual(first["device_id"], "dev123")
        await c.send(hello)
        return c, first

    async def login(self):
        c, _ = await self.connect()
        challenge = await c.recv()
        nh = auth.make_challenge()
        await c.send({"type": "proof", "nonce": nh, "proof": auth.prove(SECRET, challenge["nonce"], nh, auth.ROLE_HA)})
        proof = await c.recv()
        self.assertTrue(auth.verify(SECRET, challenge["nonce"], nh, auth.ROLE_DEVICE, proof["proof"]))
        self.snapshot_frames = [await c.recv(), await c.recv()]
        for frame in self.snapshot_frames:
            self.assertEqual(frame["type"], "state")
        return c

    async def test_non_websocket_request_is_refused(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        writer.write(b"GET /ws HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b"400", await reader.readline())
        writer.close()

    async def test_wrong_path_refused(self):
        c = await wsclient.WsTestClient.connect(self.server.port, "/other")
        self.assertIn("400", c.status)

    async def test_authenticated_session_gets_snapshot_and_commands(self):
        c = await self.login()
        await c.send({"type": "cmd", "id": 1, "name": "ok", "data": {"a": 1}})
        self.assertEqual(await c.recv(), {"type": "result", "id": 1, "ok": True, "data": {"echo": {"a": 1}}})
        self.assertEqual(self.events, [True])

    async def test_wrong_proof_is_refused(self):
        c, _ = await self.connect()
        challenge = await c.recv()
        await c.send({"type": "proof", "nonce": "00", "proof": "deadbeef"})
        self.assertEqual((await c.recv())["error"]["code"], m.UNAUTHORIZED)
        self.assertEqual(await c.recv(), ("close", 4002))

    async def test_error_mapping(self):
        c = await self.login()
        for i, (name, code) in enumerate([("bad", m.NOT_RUNNING), ("boom", m.INTERNAL), ("nope", m.UNSUPPORTED)], 1):
            await c.send({"type": "cmd", "id": i, "name": name})
            r = await c.recv()
            self.assertEqual((r["id"], r["ok"], r["error"]["code"]), (i, False, code))

    async def test_version_skew_closes_with_message(self):
        c, _ = await self.connect({"type": "hello", "proto": 0, "min_proto": 0, "device_id": "ha"})
        r = await c.recv()
        self.assertEqual((r["error"]["code"], r["peer"]), (m.VERSION_SKEW, "old"))
        self.assertEqual(await c.recv(), ("close", 4000))

    async def test_a_version_2_peer_gets_version_skew(self):
        c, _ = await self.connect({"type": "hello", "proto": 2, "min_proto": 2, "device_id": "ha"})
        r = await c.recv()
        self.assertEqual((r["error"]["code"], r["peer"]), (m.VERSION_SKEW, "old"))
        self.assertEqual(await c.recv(), ("close", 4000))

    async def test_malformed_frame_is_rejected(self):
        c = await self.login()
        await c.send("not json")
        self.assertEqual((await c.recv())["error"]["code"], m.BAD_REQUEST)

    async def test_two_rapid_launches_only_the_last_runs(self):
        c = await self.login()
        await c.send({"type": "cmd", "id": 1, "name": "slow"})   # occupies the worker
        await c.send({"type": "cmd", "id": 2, "name": "launch", "data": {"appid": "1"}})
        await c.send({"type": "cmd", "id": 3, "name": "launch", "data": {"appid": "2"}})
        superseded = await c.recv()
        self.assertEqual((superseded["id"], superseded["error"]["code"]), (2, m.BUSY))
        self.release.set()
        self.assertEqual((await c.recv())["id"], 1)
        self.assertEqual((await c.recv())["id"], 3)
        self.assertEqual(self.log, ["2"])

    async def test_new_connection_replaces_the_old_one(self):
        first = await self.login()
        second = await self.login()
        self.assertEqual(await first.recv(), ("close", 4001))
        await second.send({"type": "cmd", "id": 1, "name": "ok"})
        self.assertTrue((await second.recv())["ok"])

    async def test_pushes_reach_the_connection(self):
        c = await self.login()
        self.server.push_state("sensors", {"cpu": 1})
        self.server.push_event("toast_click", {"id": "a"})
        self.assertEqual((await c.recv())["name"], "sensors")
        self.assertEqual((await c.recv())["name"], "toast_click")

    async def test_snapshot_frames_carry_boot_id_and_seq_one(self):
        await self.login()
        self.assertEqual(len(self.server.boot_id), 16)
        self.assertEqual([(f["name"], f["boot_id"], f["seq"]) for f in self.snapshot_frames],
                         [("device", self.server.boot_id, 1), ("apps", self.server.boot_id, 1)])

    async def test_pushes_count_per_group_and_keep_the_boot_id(self):
        c = await self.login()
        for _ in range(2):
            self.server.push_state("sensors", {"cpu": 1})
        self.server.push_state("apps", {"running": True})
        frames = [await c.recv() for _ in range(3)]
        self.assertEqual([(f["name"], f["seq"]) for f in frames], [("sensors", 1), ("sensors", 2), ("apps", 2)])
        self.assertEqual({f["boot_id"] for f in frames}, {self.server.boot_id})

    async def test_snapshot_does_not_count_as_a_push(self):
        self.server.push_state("device", {"name": "x"})   # pushed once while nobody was connected
        await self.login()
        self.assertEqual({f["name"]: f["seq"] for f in self.snapshot_frames}, {"device": 1, "apps": 1})
        self.server.push_state("device", {"name": "y"})
        self.server.push_state("apps", {"running": True})
        self.assertEqual(self.server._seq, {"device": 2, "apps": 2})

    async def test_resync_answers_ok_then_sends_every_group_as_new_pushes(self):
        c = await self.login()                       # snapshot: device seq 1, apps seq 1
        await c.send({"type": "cmd", "id": 5, "name": "resync"})
        self.assertEqual(await c.recv(), {"type": "result", "id": 5, "ok": True, "data": {}})
        frames = [await c.recv(), await c.recv()]
        self.assertEqual([(f["type"], f["name"], f["seq"], f["boot_id"]) for f in frames],
                         [("state", "device", 2, self.server.boot_id), ("state", "apps", 2, self.server.boot_id)])
        self.assertEqual(frames[0]["data"], {"name": "x"})

    async def test_two_queued_resyncs_send_one_snapshot(self):
        c = await self.login()
        await c.send({"type": "cmd", "id": 1, "name": "slow"})      # occupies the worker
        await c.send({"type": "cmd", "id": 2, "name": "resync"})
        await c.send({"type": "cmd", "id": 3, "name": "resync"})
        dup = await c.recv()
        self.assertEqual((dup["id"], dup["ok"], dup["data"]), (3, True, {"coalesced": True}))
        self.release.set()
        self.assertEqual((await c.recv())["id"], 1)
        self.assertEqual((await c.recv())["id"], 2)
        self.assertEqual([(await c.recv())["type"], (await c.recv())["type"]], ["state", "state"])
        await c.send({"type": "cmd", "id": 9, "name": "ok"})          # the next frame must be this result, not a second snapshot
        self.assertEqual((await c.recv())["id"], 9)

    async def test_resync_after_a_finished_resync_runs_again(self):
        c = await self.login()
        for i in (1, 2):
            await c.send({"type": "cmd", "id": i, "name": "resync"})
            self.assertEqual((await c.recv())["id"], i)
            self.assertEqual([(await c.recv())["type"], (await c.recv())["type"]], ["state", "state"])

    async def test_shutting_down_event_reaches_the_peer(self):
        c = await self.login()
        self.server.push_event("shutting_down", {"reason": "shutdown"})
        event = await c.recv()
        self.assertEqual((event["type"], event["name"], event["data"]), ("event", "shutting_down", {"reason": "shutdown"}))

    async def test_each_server_run_has_its_own_boot_id(self):
        other = ProtocolServer(device_id=lambda: "d", secret=lambda: b"", pairing=None, commands={}, snapshot=lambda: {})
        self.assertNotEqual(other.boot_id, self.server.boot_id)

    async def test_silent_peer_is_dropped(self):
        c = await self.login()
        # never answer pings or send anything: the server must close after the idle timeout
        try:
            for _ in range(60):
                op, payload = await c.recv_raw(3)
                if op == 8:
                    break
        except asyncio.IncompleteReadError:
            pass  # the server hung up
        self.assertFalse(self.server.connected)

    async def test_unpaired_device_only_accepts_pairing(self):
        self.box["secret"] = b""
        c, hello = await self.connect()
        self.assertFalse(hello["paired"])
        await c.send({"type": "pair", "step": "begin", "code": "wrong"})
        r = await c.recv()
        self.assertEqual((r["step"], r["code"], r["attempts_left"]), ("error", m.UNAUTHORIZED, 3))
        await c.send({"type": "pair", "step": "begin", "code": "ok"})
        self.assertEqual((await c.recv())["step"], "done")
        self.assertEqual((await c.recv())["type"], "state")

    async def test_unpaired_device_refuses_commands(self):
        self.box["secret"] = b""
        c, _ = await self.connect()
        await c.send({"type": "cmd", "id": 1, "name": "ok"})
        self.assertEqual((await c.recv())["error"]["code"], m.UNAUTHORIZED)
        self.assertEqual(await c.recv(), ("close", 4002))
