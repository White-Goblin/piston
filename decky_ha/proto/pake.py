"""SRP-6a password-authenticated key exchange keyed by the pairing code (stdlib only).

The device (server) knows the code and shows it; the integration (client) is given it by the user.
Neither side sends the code or the long-term secret. A passive observer of the transcript cannot
test guesses offline (the session key needs a private exponent), and an active attacker gets one
guess per attempt, which the pairing code lifecycle limits.

Wire values are lowercase hex strings; see `to_hex` / `from_hex`.
"""
import hashlib
import hmac
import secrets

# RFC 5054 appendix A, 2048-bit group, g = 2.
_N_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A37329CBB4A099ED8193E0757767A13DD52312AB4B03310D"
    "CD7F48A9DA04FD50E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF74"
    "7359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748544523B524B0D57D"
    "5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB6"
    "94B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73"
)
N = int(_N_HEX, 16)
G = 2
N_BYTES = (N.bit_length() + 7) // 8
IDENTITY = b"decky-ha"
SECRET_LABEL = b"decky-ha-secret"


def _h(*parts: bytes) -> bytes:
    d = hashlib.sha256()
    for p in parts:
        d.update(p)
    return d.digest()


def _int(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _pad(n: int) -> bytes:
    return n.to_bytes(N_BYTES, "big")


K_MULT = _int(_h(_pad(N), _pad(G)))


def to_hex(n: int) -> str:
    return format(n, "x")


def from_hex(s: str) -> int:
    if not isinstance(s, str) or not s or len(s) > N_BYTES * 2 + 2:
        raise ValueError("bad hex value")
    return int(s, 16)


def _x(salt: bytes, code: str) -> int:
    return _int(_h(salt, _h(IDENTITY + b":" + code.encode())))


def _u(a: int, b: int) -> int:
    return _int(_h(_pad(a), _pad(b)))


def _proof_m1(a: int, b: int, salt: bytes, key: bytes) -> bytes:
    xor = bytes(x ^ y for x, y in zip(_h(_pad(N)), _h(_pad(G))))
    return _h(xor, _h(IDENTITY), salt, _pad(a), _pad(b), key)


def _proof_m2(a: int, m1: bytes, key: bytes) -> bytes:
    return _h(_pad(a), m1, key)


def derive_secret(key: bytes) -> bytes:
    """Long-term per-device secret, derived from the session key and never transmitted."""
    return hmac.new(key, SECRET_LABEL, hashlib.sha256).digest()


class PakeError(Exception):
    """Bad value from the peer or a failed proof (treated as a wrong code)."""


class SrpServer:
    """Device side. Create with the displayed code, send `salt` and `B`, then `verify` the client's A and proof."""

    def __init__(self, code: str, salt: bytes | None = None, private: int | None = None):
        self.salt = salt if salt is not None else secrets.token_bytes(16)
        self._v = pow(G, _x(self.salt, code), N)
        self._b = private if private is not None else secrets.randbits(256) | 1
        self.B = (K_MULT * self._v + pow(G, self._b, N)) % N
        self.key: bytes | None = None
        self.secret: bytes | None = None

    def verify(self, a_pub: int, m1: bytes) -> bytes:
        """Check the client's public value and proof; return M2 for the client. Raises PakeError."""
        if a_pub % N == 0:
            raise PakeError("invalid A")
        u = _u(a_pub, self.B)
        if u == 0:
            raise PakeError("invalid u")
        s = pow(a_pub * pow(self._v, u, N) % N, self._b, N)
        key = _h(_pad(s))
        if not hmac.compare_digest(m1, _proof_m1(a_pub, self.B, self.salt, key)):
            raise PakeError("wrong code")
        self.key, self.secret = key, derive_secret(key)
        return _proof_m2(a_pub, m1, key)


class SrpClient:
    """Integration side. `A` goes out first; then `process(salt, B)` yields the proof M1."""

    def __init__(self, code: str, private: int | None = None):
        self._code = code
        self._a = private if private is not None else secrets.randbits(256) | 1
        self.A = pow(G, self._a, N)
        self._b_pub = 0
        self._salt = b""
        self._m1 = b""
        self.key: bytes | None = None
        self.secret: bytes | None = None

    def process(self, salt: bytes, b_pub: int) -> bytes:
        if b_pub % N == 0:
            raise PakeError("invalid B")
        u = _u(self.A, b_pub)
        if u == 0:
            raise PakeError("invalid u")
        x = _x(salt, self._code)
        s = pow((b_pub - K_MULT * pow(G, x, N)) % N, self._a + u * x, N)
        self.key = _h(_pad(s))
        self._b_pub, self._salt = b_pub, salt
        self._m1 = _proof_m1(self.A, b_pub, salt, self.key)
        return self._m1

    def confirm(self, m2: bytes) -> None:
        """Verify the device's proof; on success `secret` is set. Raises PakeError."""
        if self.key is None or not hmac.compare_digest(m2, _proof_m2(self.A, self._m1, self.key)):
            raise PakeError("device proof mismatch")
        self.secret = derive_secret(self.key)
