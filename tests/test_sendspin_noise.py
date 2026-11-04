import json
import os
import unittest

from piston_device.sendspin import crypto, noise
from tests.sendspin_noise_vectors import VECTORS as V

h = bytes.fromhex
PROLOGUE = b'{"type":"client/init","payload":{}}{"type":"server/init","payload":{}}'


def keys():
    srv, cli = crypto.generate_private_key(), crypto.generate_private_key()
    return srv, cli, crypto.x25519_public(srv), crypto.x25519_public(cli)


def payload1(psk: bytes, category: str = "lt") -> bytes:
    return json.dumps({"psk_id": noise.psk_id_for(psk), "psk_category": category}).encode()


class Pair:
    """A handshake between our initiator and our responder with a chosen PSK on each side."""

    def __init__(self, initiator_psk, lookup=None, allow_unpaired=True, category="lt", prologue=PROLOGUE, responder_prologue=None):
        self.srv, self.cli, self.srv_pub, self.cli_pub = keys()
        self.psk = initiator_psk
        self.ini = noise.NoiseInitiator(self.srv, self.cli_pub, prologue)
        self.rsp = noise.NoiseResponder(self.cli, self.srv_pub, responder_prologue or prologue, lookup, allow_unpaired)
        self.msg1 = self.ini.write_message_1(payload1(initiator_psk, category))

    def finish(self):
        self.rsp.read_message_1(self.msg1)
        msg2, t_rsp = self.rsp.write_message_2()
        payload, t_ini = self.ini.read_message_2(msg2, self.psk)
        return payload, t_ini, t_rsp


class KnownAnswerTests(unittest.TestCase):
    def responder(self, case):
        v = V[case]
        lookup = lambda psk_id, category: h(v["psk"]) if psk_id == noise.psk_id_for(h(v["psk"])) else None
        return noise.NoiseResponder(h(V["client_static_private"]), h(V["server_static_public"]), h(v["prologue"]), lookup,
                                    ephemeral_private=h(V["client_ephemeral"])), v

    def test_public_keys_match_the_generator(self):
        self.assertEqual(crypto.x25519_public(h(V["server_static_private"])).hex(), V["server_static_public"])
        self.assertEqual(crypto.x25519_public(h(V["client_static_private"])).hex(), V["client_static_public"])

    def test_a_named_psk_transcript_matches_the_reference_library_byte_for_byte(self):
        rsp, v = self.responder("named")
        self.assertEqual(rsp.read_message_1(h(v["msg1"])), json.loads(h(v["payload1"])))
        self.assertFalse(rsp.unpaired)
        msg2, t = rsp.write_message_2(h(v["payload2"]))
        self.assertEqual(msg2.hex(), v["msg2"])
        self.assertEqual(t.send(b"client to server one").hex(), v["c2s_1"])
        self.assertEqual(t.receive(h(v["s2c_1"])), b"server to client one")
        self.assertEqual(t.receive(h(v["s2c_2"])), b"server to client two")

    def test_a_sentinel_transcript_matches_too(self):
        rsp, v = self.responder("sentinel")
        self.assertEqual(h(v["psk"]), noise.SENTINEL_PSK)
        rsp.read_message_1(h(v["msg1"]))
        self.assertTrue(rsp.unpaired)  # lookup knows no id, so the Sentinel PSK is used
        msg2, t = rsp.write_message_2()
        self.assertEqual(msg2.hex(), v["msg2"])
        self.assertEqual(t.receive(h(v["s2c_1"])), b"server to client one")

    def test_our_initiator_reproduces_the_reference_message_1(self):
        v = V["named"]
        ini = noise.NoiseInitiator(h(V["server_static_private"]), h(V["client_static_public"]), h(v["prologue"]),
                                   ephemeral_private=h(V["server_ephemeral"]))
        self.assertEqual(ini.write_message_1(h(v["payload1"])).hex(), v["msg1"])
        payload, t = ini.read_message_2(h(v["msg2"]), h(v["psk"]))
        self.assertEqual(payload, b"{}")
        self.assertEqual(t.send(b"server to client one").hex(), v["s2c_1"])
        self.assertEqual(t.receive(h(v["c2s_1"])), b"client to server one")


class HandshakeTests(unittest.TestCase):
    def test_a_handshake_completes_and_transport_works_both_ways(self):
        psk = os.urandom(32)
        pair = Pair(psk, lookup=lambda i, c: psk if i == noise.psk_id_for(psk) else None)
        payload, t_ini, t_rsp = pair.finish()
        self.assertEqual((payload, pair.rsp.psk_category, pair.rsp.unpaired), (b"{}", "lt", False))
        for i in range(5):
            self.assertEqual(t_rsp.receive(t_ini.send(b"audio %d" % i)), b"audio %d" % i)
            self.assertEqual(t_ini.receive(t_rsp.send(b"state %d" % i)), b"state %d" % i)

    def test_an_unknown_psk_id_falls_back_to_the_sentinel(self):
        pair = Pair(noise.SENTINEL_PSK, category="sn")
        payload, t_ini, t_rsp = pair.finish()
        self.assertTrue(pair.rsp.unpaired)
        self.assertEqual(t_rsp.receive(t_ini.send(b"x")), b"x")

    def test_without_unpaired_access_an_unknown_psk_fails(self):
        pair = Pair(noise.SENTINEL_PSK, allow_unpaired=False, category="sn")
        with self.assertRaises(noise.NoiseError):
            pair.rsp.read_message_1(pair.msg1)

    def test_a_server_using_a_different_psk_than_it_announced_is_detected_by_the_initiator(self):
        psk, other = os.urandom(32), os.urandom(32)
        pair = Pair(psk, lookup=lambda i, c: other)  # the responder mixes a different PSK
        pair.rsp.read_message_1(pair.msg1)
        msg2, _ = pair.rsp.write_message_2()
        with self.assertRaises(noise.NoiseError):
            pair.ini.read_message_2(msg2, psk)

    def test_a_mismatched_prologue_fails_message_1(self):
        pair = Pair(noise.SENTINEL_PSK, category="sn", responder_prologue=PROLOGUE + b"x")
        with self.assertRaises(noise.NoiseError):
            pair.rsp.read_message_1(pair.msg1)

    def test_a_server_with_another_static_key_fails_message_1(self):
        pair = Pair(noise.SENTINEL_PSK, category="sn")
        rsp = noise.NoiseResponder(pair.cli, crypto.x25519_public(crypto.generate_private_key()), PROLOGUE)
        with self.assertRaises(noise.NoiseError):
            rsp.read_message_1(pair.msg1)

    def test_any_tampered_byte_of_message_1_fails(self):
        pair = Pair(noise.SENTINEL_PSK, category="sn")
        for i in (0, 31, 32, len(pair.msg1) - 1):
            bad = bytearray(pair.msg1)
            bad[i] ^= 1
            rsp = noise.NoiseResponder(pair.cli, pair.srv_pub, PROLOGUE)
            with self.assertRaises(noise.NoiseError, msg=f"byte {i}"):
                rsp.read_message_1(bytes(bad))

    def test_malformed_input_fails_cleanly(self):
        pair = Pair(noise.SENTINEL_PSK, category="sn")
        for msg in (b"", b"short", bytes(31), bytes(32), os.urandom(200), bytes(65536)):
            rsp = noise.NoiseResponder(pair.cli, pair.srv_pub, PROLOGUE)
            with self.assertRaises(noise.NoiseError, msg=f"{len(msg)} bytes"):
                rsp.read_message_1(msg)

    def test_low_order_ephemeral_keys_are_refused(self):
        pair = Pair(noise.SENTINEL_PSK, category="sn")
        rsp = noise.NoiseResponder(pair.cli, pair.srv_pub, PROLOGUE)
        with self.assertRaises(noise.NoiseError):
            rsp.read_message_1(bytes(32) + pair.msg1[32:])

    def test_payload_must_name_a_psk(self):
        srv, cli, srv_pub, cli_pub = keys()
        for raw in (b"not json", b"{}", b'{"psk_id":"x","psk_category":"lt"}', json.dumps({"psk_id": "A" * 43, "psk_category": "zz"}).encode(),
                    json.dumps({"psk_id": 5, "psk_category": "lt"}).encode(), b"[]"):
            ini = noise.NoiseInitiator(srv, cli_pub, PROLOGUE)
            rsp = noise.NoiseResponder(cli, srv_pub, PROLOGUE)
            with self.assertRaises(noise.NoiseError, msg=raw):
                rsp.read_message_1(ini.write_message_1(raw))

    def test_message_2_needs_message_1_first(self):
        _, cli, srv_pub, _ = keys()
        with self.assertRaises(noise.NoiseError):
            noise.NoiseResponder(cli, srv_pub, PROLOGUE).write_message_2()

    def test_a_psk_of_the_wrong_length_is_refused(self):
        pair = Pair(os.urandom(32), lookup=lambda i, c: b"short")
        with self.assertRaises(noise.NoiseError):
            pair.rsp.read_message_1(pair.msg1)

    def test_psk_id_of_the_sentinel_matches_the_spec(self):
        self.assertEqual(noise.SENTINEL_PSK.hex(), "1b5e24dbc1aed95fc2a5a338a90c05df44bd10f5ec1f4cd66cbf86272767b9d3")
        self.assertEqual(noise.SENTINEL_PSK_ID, "GFsV9tLaSQm9HcFWpKsgYQOr7wFTvNUtkmFwuVz3zoo")


class TransportTests(unittest.TestCase):
    def setUp(self):
        _, self.ini, self.rsp = Pair(noise.SENTINEL_PSK, category="sn").finish()

    def test_replayed_and_out_of_order_messages_fail(self):
        a, b, c = (self.ini.send(x) for x in (b"one", b"two", b"three"))
        self.assertEqual(self.rsp.receive(a), b"one")
        with self.assertRaises(noise.NoiseError):
            self.rsp.receive(a)  # replay
        with self.assertRaises(noise.NoiseError):
            self.rsp.receive(c)  # out of order (skips b)

    def test_a_failed_message_does_not_advance_the_counter(self):
        a = self.ini.send(b"one")
        with self.assertRaises(noise.NoiseError):
            self.rsp.receive(a[:-1] + bytes([a[-1] ^ 1]))
        self.assertEqual(self.rsp.receive(a), b"one")

    def test_size_limits(self):
        self.assertEqual(self.rsp.receive(self.ini.send(bytes(noise.MAX_PLAINTEXT))), bytes(noise.MAX_PLAINTEXT))
        with self.assertRaises(noise.NoiseError):
            self.ini.send(bytes(noise.MAX_PLAINTEXT + 1))
        for bad in (b"", bytes(15), bytes(noise.MAX_MESSAGE + 1)):
            with self.assertRaises(noise.NoiseError):
                self.rsp.receive(bad)

    def test_directions_use_different_keys(self):
        message = self.ini.send(b"x")
        with self.assertRaises(noise.NoiseError):
            self.ini.receive(message)


if __name__ == "__main__":
    unittest.main()
