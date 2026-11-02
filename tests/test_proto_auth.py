import unittest

from decky_ha.proto import auth

SECRET = b"s" * 32


class AuthTests(unittest.TestCase):
    def test_challenges_are_unique_hex(self):
        a, b = auth.make_challenge(), auth.make_challenge()
        self.assertNotEqual(a, b)
        int(a, 16)
        self.assertEqual(len(a), 32)

    def test_right_secret_passes_both_ways(self):
        nd, nh = auth.make_challenge(), auth.make_challenge()
        self.assertTrue(auth.verify(SECRET, nd, nh, auth.ROLE_HA, auth.prove(SECRET, nd, nh, auth.ROLE_HA)))
        self.assertTrue(auth.verify(SECRET, nd, nh, auth.ROLE_DEVICE, auth.prove(SECRET, nd, nh, auth.ROLE_DEVICE)))

    def test_wrong_secret_fails(self):
        nd, nh = auth.make_challenge(), auth.make_challenge()
        self.assertFalse(auth.verify(b"x" * 32, nd, nh, auth.ROLE_HA, auth.prove(SECRET, nd, nh, auth.ROLE_HA)))

    def test_reflection_fails(self):
        nd, nh = auth.make_challenge(), auth.make_challenge()
        self.assertFalse(auth.verify(SECRET, nd, nh, auth.ROLE_DEVICE, auth.prove(SECRET, nd, nh, auth.ROLE_HA)))
        self.assertFalse(auth.verify(SECRET, nd, nh, auth.ROLE_HA, auth.prove(SECRET, nh, nd, auth.ROLE_HA)))

    def test_replay_with_new_nonce_fails(self):
        nd, nh = auth.make_challenge(), auth.make_challenge()
        proof = auth.prove(SECRET, nd, nh, auth.ROLE_HA)
        self.assertFalse(auth.verify(SECRET, auth.make_challenge(), nh, auth.ROLE_HA, proof))

    def test_non_string_proof_and_bad_role(self):
        self.assertFalse(auth.verify(SECRET, "a", "b", auth.ROLE_HA, None))
        with self.assertRaises(ValueError):
            auth.prove(SECRET, "a", "b", b"x")
