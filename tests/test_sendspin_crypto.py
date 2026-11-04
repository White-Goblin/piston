import unittest
from unittest import mock

from piston_device.sendspin import crypto

h = bytes.fromhex


class CryptoTests(unittest.TestCase):
    def test_the_self_test_passes_on_this_host(self):
        crypto.self_test()
        crypto.load()

    def test_x25519_rfc7748_vectors(self):
        alice = h("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        bob = h("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
        alice_pub, bob_pub = crypto.x25519_public(alice), crypto.x25519_public(bob)
        self.assertEqual(alice_pub.hex(), "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
        self.assertEqual(bob_pub.hex(), "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
        shared = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
        self.assertEqual(crypto.x25519(alice, bob_pub).hex(), shared)
        self.assertEqual(crypto.x25519(bob, alice_pub).hex(), shared)

    def test_a_low_order_public_key_is_refused(self):
        with self.assertRaises(crypto.CryptoError):
            crypto.x25519(crypto.generate_private_key(), bytes(32))

    def test_bad_key_lengths_are_refused(self):
        for call in (lambda: crypto.x25519_public(b"short"), lambda: crypto.x25519(bytes(32), bytes(31)),
                     lambda: crypto.aead_encrypt(bytes(31), bytes(12), b"", b""), lambda: crypto.aead_encrypt(bytes(32), bytes(8), b"", b"")):
            with self.assertRaises(crypto.CryptoError):
                call()

    def test_chacha20_poly1305_rfc8439_vector(self):
        key = h("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
        nonce, ad = h("070000004041424344454647"), h("50515253c0c1c2c3c4c5c6c7")
        pt = b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it."
        ct = crypto.aead_encrypt(key, nonce, ad, pt)
        self.assertEqual(ct[:16].hex(), "d31a8d34648e60db7b86afbc53ef7ec2")
        self.assertEqual(ct[-16:].hex(), "1ae10b594f09e26a7e902ecbd0600691")
        self.assertEqual(crypto.aead_decrypt(key, nonce, ad, ct), pt)

    def test_round_trips_including_empty_and_large_payloads(self):
        key, nonce = crypto.generate_private_key(), bytes(12)
        for ad in (b"", b"header"):
            for size in (0, 1, 16, 3840, 65519):
                data = bytes(range(256)) * (size // 256) + bytes(size % 256)
                self.assertEqual(crypto.aead_decrypt(key, nonce, ad, crypto.aead_encrypt(key, nonce, ad, data)), data)

    def test_tampering_with_any_part_is_detected(self):
        key, nonce = crypto.generate_private_key(), bytes(12)
        ct = crypto.aead_encrypt(key, nonce, b"ad", b"secret audio")
        for bad in (ct[:-1] + bytes([ct[-1] ^ 1]), bytes([ct[0] ^ 1]) + ct[1:], ct[:-1], b"short"):
            with self.assertRaises(ValueError):
                crypto.aead_decrypt(key, nonce, b"ad", bad)
        with self.assertRaises(ValueError):
            crypto.aead_decrypt(key, nonce, b"other", ct)
        with self.assertRaises(ValueError):
            crypto.aead_decrypt(key, bytes([1]) + bytes(11), b"ad", ct)

    def test_hkdf_rfc5869_case_1_and_hmac(self):
        okm = crypto.hkdf(h("000102030405060708090a0b0c"), bytes([0x0b] * 22), h("f0f1f2f3f4f5f6f7f8f9"), 42)
        self.assertEqual(okm.hex(), "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865")
        self.assertEqual(crypto.hmac_sha256(b"Jefe", b"what do ya want for nothing?").hex(),
                         "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843")
        with self.assertRaises(crypto.CryptoError):
            crypto.hkdf(b"", b"x", b"", 0)

    def test_an_unloadable_library_raises_crypto_unavailable(self):
        with mock.patch.object(crypto, "_lib", None), mock.patch("ctypes.CDLL", side_effect=OSError("no such file")):
            with self.assertRaises(crypto.CryptoUnavailable) as cm:
                crypto.load()
            self.assertIn("no such file", str(cm.exception))

    def test_a_failing_self_test_marks_the_library_unusable(self):
        with mock.patch.object(crypto, "_lib", None), mock.patch.object(crypto, "self_test", side_effect=crypto.CryptoUnavailable("bad")):
            with self.assertRaises(crypto.CryptoUnavailable):
                crypto.load()
            self.assertIsNone(crypto._lib)


if __name__ == "__main__":
    unittest.main()
