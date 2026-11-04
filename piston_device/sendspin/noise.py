"""Noise_KKpsk2_25519_ChaChaPoly_SHA256 for Sendspin (Noise revision 34), both roles.

Sendspin's server is always the initiator and the client the responder, so the plugin uses `NoiseResponder`; the initiator
exists for tests. Both static keys are known beforehand (the client_id and server_id are the public keys). The pattern:

    -> s / <- s            (pre-messages: both static public keys are mixed into the hash)
    -> e, es, ss           message 1 (initiator to responder), the payload is already encrypted
    <- e, ee, se, psk      message 2 (responder to initiator)

Because the pattern has a psk token, every `e` also calls MixKey(e.public). The payload of message 1 names the PSK
(`psk_id`, `psk_category`); the responder picks the PSK, falling back to the Sentinel PSK for an unknown id (unpaired
access), and mixes it in at the end of message 2."""
import base64
import hashlib
import json
import struct

from . import crypto

PROTOCOL_NAME = b"Noise_KKpsk2_25519_ChaChaPoly_SHA256"
MAX_MESSAGE = 65535
MAX_PLAINTEXT = MAX_MESSAGE - crypto.TAG_LEN
DH_LEN = 32
SENTINEL_PSK = hashlib.sha256(b"sendspin-sentinel-psk-v1").digest()
PSK_CATEGORIES = ("lt", "pr", "sn")


class NoiseError(Exception):
    """The handshake or a transport message is invalid (wrong key, tampering, replay, bad size)."""


def psk_id_for(psk: bytes) -> str:
    """base64url (no padding) of SHA-256("sendspin-psk-id-v1" || PSK), as the spec defines it."""
    return base64.urlsafe_b64encode(hashlib.sha256(b"sendspin-psk-id-v1" + psk).digest()).rstrip(b"=").decode()


SENTINEL_PSK_ID = psk_id_for(SENTINEL_PSK)


def _hkdf(chaining_key: bytes, ikm: bytes, outputs: int) -> tuple:
    """Noise HKDF: HMAC-SHA256 chain, two or three outputs."""
    temp = crypto.hmac_sha256(chaining_key, ikm)
    out, previous = [], b""
    for i in range(1, outputs + 1):
        previous = crypto.hmac_sha256(temp, previous + bytes([i]))
        out.append(previous)
    return tuple(out)


def _nonce(n: int) -> bytes:
    return bytes(4) + struct.pack("<Q", n)


class _Symmetric:
    def __init__(self):
        name = PROTOCOL_NAME
        self.h = name.ljust(32, b"\0") if len(name) <= 32 else hashlib.sha256(name).digest()
        self.ck = self.h
        self.k = None
        self.n = 0

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, self.k = _hkdf(self.ck, ikm, 2)
        self.n = 0

    def mix_key_and_hash(self, ikm: bytes) -> None:
        self.ck, temp_h, self.k = _hkdf(self.ck, ikm, 3)
        self.mix_hash(temp_h)
        self.n = 0

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        if self.k is None:
            ciphertext = plaintext
        else:
            ciphertext = crypto.aead_encrypt(self.k, _nonce(self.n), self.h, plaintext)
            self.n += 1
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        if self.k is None:
            plaintext = ciphertext
        else:
            try:
                plaintext = crypto.aead_decrypt(self.k, _nonce(self.n), self.h, ciphertext)
            except crypto.CryptoError:
                raise NoiseError("handshake message failed authentication") from None
            self.n += 1
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple:
        return _hkdf(self.ck, b"", 2)


class NoiseTransport:
    """The two cipher states after the handshake. Nonces count up; a repeated or out-of-order message fails to decrypt."""

    def __init__(self, send_key: bytes, receive_key: bytes):
        self._send_key, self._receive_key = send_key, receive_key
        self._send_n = self._receive_n = 0

    def send(self, plaintext: bytes) -> bytes:
        if len(plaintext) > MAX_PLAINTEXT:
            raise NoiseError("message too large")
        if self._send_n >= 2**64 - 1:
            raise NoiseError("nonce exhausted")
        out = crypto.aead_encrypt(self._send_key, _nonce(self._send_n), b"", bytes(plaintext))
        self._send_n += 1
        return out

    def receive(self, ciphertext: bytes) -> bytes:
        if not crypto.TAG_LEN <= len(ciphertext) <= MAX_MESSAGE:
            raise NoiseError("bad message size")
        try:
            out = crypto.aead_decrypt(self._receive_key, _nonce(self._receive_n), b"", bytes(ciphertext))
        except crypto.CryptoError:
            raise NoiseError("message failed authentication") from None
        self._receive_n += 1
        return out


class _Handshake:
    initiator = False

    def __init__(self, static_private: bytes, remote_static_public: bytes, prologue: bytes, ephemeral_private: bytes | None = None):
        """`ephemeral_private` is for tests only (fixed vectors); in use it is generated per handshake."""
        self._s = static_private
        self._s_pub = crypto.x25519_public(static_private)
        self._rs = remote_static_public
        self._e = ephemeral_private if ephemeral_private is not None else crypto.generate_private_key()
        self._e_pub = crypto.x25519_public(self._e)
        self._re = None
        self.sym = _Symmetric()
        self.sym.mix_hash(prologue)
        first, second = (self._s_pub, self._rs) if self.initiator else (self._rs, self._s_pub)
        self.sym.mix_hash(first)   # -> s (the initiator's static key)
        self.sym.mix_hash(second)  # <- s (the responder's static key)

    def _dh(self, private: bytes, public: bytes) -> bytes:
        try:
            return crypto.x25519(private, public)
        except crypto.CryptoError:
            raise NoiseError("invalid public key") from None

    def _write_e(self) -> bytes:
        self.sym.mix_hash(self._e_pub)
        self.sym.mix_key(self._e_pub)  # psk modifier
        return self._e_pub

    def _read_e(self, message: bytes) -> bytes:
        if len(message) < DH_LEN:
            raise NoiseError("handshake message too short")
        self._re = bytes(message[:DH_LEN])
        self.sym.mix_hash(self._re)
        self.sym.mix_key(self._re)
        return bytes(message[DH_LEN:])

    def _check(self, message: bytes) -> None:
        if len(message) > MAX_MESSAGE:
            raise NoiseError("handshake message too large")

    def _transport(self) -> NoiseTransport:
        c1, c2 = self.sym.split()
        return NoiseTransport(c1, c2) if self.initiator else NoiseTransport(c2, c1)


class NoiseInitiator(_Handshake):
    """The server's side (tests only in the plugin)."""

    initiator = True

    def write_message_1(self, payload: bytes) -> bytes:
        out = self._write_e()
        self.sym.mix_key(self._dh(self._e, self._rs))  # es
        self.sym.mix_key(self._dh(self._s, self._rs))  # ss
        out += self.sym.encrypt_and_hash(payload)
        self._check(out)
        return out

    def read_message_2(self, message: bytes, psk: bytes) -> tuple:
        """Returns (payload, transport)."""
        self._check(message)
        rest = self._read_e(message)
        self.sym.mix_key(self._dh(self._e, self._re))  # ee
        self.sym.mix_key(self._dh(self._s, self._re))  # se
        self.sym.mix_key_and_hash(psk)
        return self.sym.decrypt_and_hash(rest), self._transport()


class NoiseResponder(_Handshake):
    """The client's side: reads message 1, picks the PSK, writes message 2.

    `psk_lookup(psk_id, category)` returns the 32-byte PSK for a known id, or None. An unknown id falls back to the
    Sentinel PSK when `allow_unpaired` is true (D11); otherwise the handshake fails."""

    def __init__(self, static_private: bytes, initiator_static_public: bytes, prologue: bytes, psk_lookup=None,
                 allow_unpaired: bool = True, ephemeral_private: bytes | None = None):
        super().__init__(static_private, initiator_static_public, prologue, ephemeral_private)
        self._lookup = psk_lookup or (lambda psk_id, category: None)
        self._allow_unpaired = allow_unpaired
        self.psk_id = self.psk_category = None
        self._psk = None

    def read_message_1(self, message: bytes) -> dict:
        """Returns the decoded payload `{psk_id, psk_category}`; raises NoiseError on any failure."""
        self._check(message)
        rest = self._read_e(message)
        self.sym.mix_key(self._dh(self._s, self._re))  # es
        self.sym.mix_key(self._dh(self._s, self._rs))  # ss
        raw = self.sym.decrypt_and_hash(rest)
        try:
            payload = json.loads(raw.decode("utf-8"))
            psk_id, category = payload["psk_id"], payload["psk_category"]
        except (ValueError, KeyError, TypeError):
            raise NoiseError("message 1 payload is not valid") from None
        if not isinstance(psk_id, str) or len(psk_id) != 43 or category not in PSK_CATEGORIES:
            raise NoiseError("message 1 payload is not valid")
        psk = self._lookup(psk_id, category)
        if psk is None:
            if not self._allow_unpaired:
                raise NoiseError("unknown PSK and unpaired access is off")
            psk = SENTINEL_PSK
        elif len(psk) != 32:
            raise NoiseError("PSK must be 32 bytes")
        self.psk_id, self.psk_category, self._psk = psk_id, category, psk
        return payload

    @property
    def unpaired(self) -> bool:
        """True when the handshake will use the Sentinel PSK (no authentication)."""
        return self._psk == SENTINEL_PSK

    def write_message_2(self, payload: bytes = b"{}") -> tuple:
        """Returns (message, transport); the payload is the literal `{}` in Sendspin."""
        if self._psk is None:
            raise NoiseError("read message 1 first")
        out = self._write_e()
        self.sym.mix_key(self._dh(self._e, self._re))  # ee
        self.sym.mix_key(self._dh(self._e, self._rs))  # se
        self.sym.mix_key_and_hash(self._psk)
        out += self.sym.encrypt_and_hash(payload)
        self._check(out)
        return out, self._transport()
