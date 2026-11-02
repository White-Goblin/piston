import hashlib
import unittest

from decky_ha.proto import pake


def run(code_dev="123456", code_user="123456", a=None, b=None, salt=b"S" * 16):
    server = pake.SrpServer(code_dev, salt=salt, private=b)
    client = pake.SrpClient(code_user, private=a)
    m1 = client.process(server.salt, server.B)
    m2 = server.verify(client.A, m1)
    client.confirm(m2)
    return server, client


class PakeTests(unittest.TestCase):
    def test_same_code_same_secret(self):
        server, client = run()
        self.assertEqual(server.key, client.key)
        self.assertEqual(server.secret, client.secret)
        self.assertEqual(len(server.secret), 32)

    def test_wrong_code_fails_both_ways(self):
        server = pake.SrpServer("123456")
        client = pake.SrpClient("654321")
        m1 = client.process(server.salt, server.B)
        with self.assertRaises(pake.PakeError):
            server.verify(client.A, m1)
        self.assertIsNone(server.secret)
        with self.assertRaises(pake.PakeError):
            client.confirm(b"\0" * 32)
        self.assertIsNone(client.secret)

    def test_rejects_degenerate_public_values(self):
        server = pake.SrpServer("123456")
        for bad in (0, pake.N, 2 * pake.N):
            with self.assertRaises(pake.PakeError):
                server.verify(bad, b"x" * 32)
        client = pake.SrpClient("123456")
        with self.assertRaises(pake.PakeError):
            client.process(b"s" * 16, pake.N)

    def test_fresh_sessions_give_different_secrets(self):
        self.assertNotEqual(run()[0].secret, run()[0].secret)

    def test_known_answer_vector(self):
        # Fixed salt and private exponents make the exchange deterministic. The expected values are
        # recomputed here straight from the SRP-6a formulas, so the implementation is cross-checked.
        N, g, H = pake.N, pake.G, lambda *p: hashlib.sha256(b"".join(p)).digest()
        pad = lambda n: n.to_bytes(256, "big")
        salt, a, b, code = b"S" * 16, 0x1234567890ABCDEF1234567890ABCDEF, 0xFEDCBA0987654321FEDCBA0987654321, "246810"
        x = int.from_bytes(H(salt, H(b"decky-ha:" + code.encode())), "big")
        v = pow(g, x, N)
        k = int.from_bytes(H(pad(N), pad(g)), "big")
        A, B = pow(g, a, N), (k * v + pow(g, b, N)) % N
        u = int.from_bytes(H(pad(A), pad(B)), "big")
        S = pow(A * pow(v, u, N) % N, b, N)
        expected_key = H(pad(S))
        server, client = run(code, code, a, b, salt)
        self.assertEqual(server.B, B)
        self.assertEqual(client.A, A)
        self.assertEqual(server.key, expected_key)
        self.assertEqual(client.key, expected_key)

    def test_hex_helpers(self):
        self.assertEqual(pake.from_hex(pake.to_hex(2 ** 255 + 7)), 2 ** 255 + 7)
        for bad in ("", "zz", "f" * 600, 5):
            with self.assertRaises(ValueError):
                pake.from_hex(bad)


if __name__ == "__main__":
    unittest.main()
