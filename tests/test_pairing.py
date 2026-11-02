import unittest

from decky_ha.pairing import LOCKOUT_SECONDS, PairingManager
from decky_ha.proto import code as code_mod, messages as m, pake


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class PairingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.secret = b""
        self.changes = 0
        self.clock = Clock()

        def store(s):
            self.secret = s

        def changed():
            self.changes += 1

        self.mgr = PairingManager(lambda: self.secret, store, changed, self.clock,
                                  code_mod.PairingCode(clock=self.clock))

    async def pair(self, code):
        """Run the integration side against the manager; returns the client."""
        challenge = await self.mgr.handle({"type": "pair", "step": "begin"})
        client = pake.SrpClient(code)
        m1 = client.process(bytes.fromhex(challenge["salt"]), pake.from_hex(challenge["B"]))
        done = await self.mgr.handle({"type": "pair", "step": "proof", "A": pake.to_hex(client.A), "M1": m1.hex()})
        client.confirm(bytes.fromhex(done["M2"]))
        return client

    async def test_state_shows_code_only_while_unpaired(self):
        s = self.mgr.state()
        self.assertEqual((s["paired"], len(s["code"]), s["attempts_left"]), (False, 6, 5))

    async def test_right_code_stores_the_same_secret_on_both_sides(self):
        client = await self.pair(self.mgr.code.code)
        self.assertEqual(self.secret, client.secret)
        self.assertEqual(self.mgr.state(), {"paired": True})

    async def test_wrong_code_is_counted_and_secret_not_stored(self):
        with self.assertRaises(m.ProtocolError) as cm:
            await self.pair("000000" if self.mgr.code.code != "000000" else "111111")
        self.assertEqual((cm.exception.data["reason"], cm.exception.data["attempts_left"]), ("wrong_code", 4))
        self.assertEqual(self.secret, b"")

    async def test_five_wrong_attempts_lock_then_recover_after_lockout(self):
        wrong = "000000" if self.mgr.code.code != "000000" else "111111"
        for _ in range(5):
            with self.assertRaises(m.ProtocolError):
                await self.pair(wrong)
        with self.assertRaises(m.ProtocolError) as cm:
            await self.mgr.handle({"type": "pair", "step": "begin"})
        self.assertEqual(cm.exception.data["reason"], "locked")
        self.clock.t += LOCKOUT_SECONDS
        old = self.mgr.code.code
        client = await self.pair(self.mgr.state()["code"])
        self.assertEqual(self.secret, client.secret)
        self.assertIsNotNone(old)

    async def test_expired_code_is_replaced(self):
        old = self.mgr.code.code
        self.clock.t += 301
        state = self.mgr.state()
        self.assertEqual(len(state["code"]), 6)
        self.assertEqual(state["attempts_left"], 5)
        client = await self.pair(state["code"])
        self.assertTrue(client.secret)
        self.assertIsNotNone(old)

    async def test_already_paired_refuses(self):
        await self.pair(self.mgr.code.code)
        with self.assertRaises(m.ProtocolError) as cm:
            await self.mgr.handle({"type": "pair", "step": "begin"})
        self.assertEqual(cm.exception.data["reason"], "already_paired")

    async def test_proof_without_begin_and_garbage(self):
        with self.assertRaises(m.ProtocolError):
            await self.mgr.handle({"type": "pair", "step": "proof", "A": "1", "M1": "00"})
        await self.mgr.handle({"type": "pair", "step": "begin"})
        with self.assertRaises(m.ProtocolError) as cm:
            await self.mgr.handle({"type": "pair", "step": "proof", "A": "zz", "M1": "nothex"})
        self.assertEqual(cm.exception.data["reason"], "wrong_code")
        with self.assertRaises(m.ProtocolError):
            await self.mgr.handle({"type": "pair", "step": "other"})

    async def test_unpair_clears_secret_and_makes_a_new_code(self):
        await self.pair(self.mgr.code.code)
        self.mgr.unpair()
        self.assertEqual(self.secret, b"")
        s = self.mgr.state()
        self.assertFalse(s["paired"])
        self.assertEqual(len(s["code"]), 6)
        self.assertGreater(self.changes, 0)


class PairingOverWebsocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_pairing_then_authenticated_reconnect(self):
        from decky_ha.proto import PROTOCOL_VERSION, auth
        from decky_ha.server import ProtocolServer
        from . import wsclient

        box = {"secret": b""}
        mgr = PairingManager(lambda: box["secret"], lambda s: box.update(secret=s))
        server = ProtocolServer(device_id=lambda: "dev", secret=lambda: box["secret"], pairing=mgr,
                                commands={}, snapshot=lambda: {"device": {}}, host="127.0.0.1", port=0)
        await server.start()
        self.addAsyncCleanup(server.stop)
        hello = {"type": "hello", "proto": PROTOCOL_VERSION, "min_proto": 1, "device_id": "ha"}

        c = await wsclient.WsTestClient.connect(server.port)
        await c.recv()
        await c.send(hello)
        await c.send({"type": "pair", "step": "begin"})
        ch = await c.recv()
        client = pake.SrpClient(mgr.code.code)
        m1 = client.process(bytes.fromhex(ch["salt"]), pake.from_hex(ch["B"]))
        await c.send({"type": "pair", "step": "proof", "A": pake.to_hex(client.A), "M1": m1.hex()})
        done = await c.recv()
        client.confirm(bytes.fromhex(done["M2"]))
        self.assertEqual((await c.recv())["type"], "state")
        c.close()

        c2 = await wsclient.WsTestClient.connect(server.port)
        self.assertTrue((await c2.recv())["paired"])
        await c2.send(hello)
        challenge = await c2.recv()
        nh = auth.make_challenge()
        await c2.send({"type": "proof", "nonce": nh, "proof": auth.prove(client.secret, challenge["nonce"], nh, auth.ROLE_HA)})
        self.assertEqual((await c2.recv())["type"], "proof")
        c2.close()
