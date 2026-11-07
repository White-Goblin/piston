"""X25519 and ChaCha20-Poly1305 through ctypes over the system libcrypto (OpenSSL 3); HMAC and HKDF from the stdlib.

Why: the plugin has no dependencies and Python's standard library has neither primitive; the device's own OpenSSL does.
Everything that touches ctypes is in this module. Keys are plain bytes and are
never logged. `self_test()` runs the RFC vectors and is the check that the loaded library really works."""
import ctypes
import ctypes.util
import hashlib
import hmac
import os

KEY_LEN = 32
TAG_LEN = 16
NONCE_LEN = 12


class CryptoUnavailable(Exception):
    """libcrypto could not be loaded or failed the self-test; the Sendspin player cannot run."""


class CryptoError(ValueError):
    """An operation failed: a bad tag, a bad key, or a low-order public key."""


_NID_X25519 = 1034
_CTRL_SET_IVLEN, _CTRL_GET_TAG, _CTRL_SET_TAG = 0x9, 0x10, 0x11
_lib = None


def _declare(lib):
    vp, ci, st, cp, ip = ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)
    sizes = {
        "EVP_PKEY_new_raw_private_key": (vp, [ci, vp, cp, st]),
        "EVP_PKEY_new_raw_public_key": (vp, [ci, vp, cp, st]),
        "EVP_PKEY_get_raw_public_key": (ci, [vp, cp, ctypes.POINTER(st)]),
        "EVP_PKEY_free": (None, [vp]),
        "EVP_PKEY_CTX_new": (vp, [vp, vp]),
        "EVP_PKEY_CTX_free": (None, [vp]),
        "EVP_PKEY_derive_init": (ci, [vp]),
        "EVP_PKEY_derive_set_peer": (ci, [vp, vp]),
        "EVP_PKEY_derive": (ci, [vp, cp, ctypes.POINTER(st)]),
        "EVP_CIPHER_fetch": (vp, [vp, cp, vp]),
        "EVP_CIPHER_free": (None, [vp]),
        "EVP_CIPHER_CTX_new": (vp, []),
        "EVP_CIPHER_CTX_free": (None, [vp]),
        "EVP_CIPHER_CTX_ctrl": (ci, [vp, ci, ci, cp]),
        # (ctx, cipher, key, iv, params): the order matters, a swap crashes inside OpenSSL
        "EVP_EncryptInit_ex2": (ci, [vp, vp, cp, cp, vp]),
        "EVP_DecryptInit_ex2": (ci, [vp, vp, cp, cp, vp]),
        "EVP_EncryptUpdate": (ci, [vp, cp, ip, cp, ci]),
        "EVP_DecryptUpdate": (ci, [vp, cp, ip, cp, ci]),
        "EVP_EncryptFinal_ex": (ci, [vp, cp, ip]),
        "EVP_DecryptFinal_ex": (ci, [vp, cp, ip]),
    }
    for name, (res, args) in sizes.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args


def load():
    """The declared libcrypto, loaded once; raises CryptoUnavailable (with the reason) if it cannot be used."""
    global _lib
    if _lib is not None:
        return _lib
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("crypto") or "libcrypto.so.3")
        _declare(lib)
    except (OSError, AttributeError) as e:
        raise CryptoUnavailable(f"libcrypto is not usable: {e}") from None
    _lib = lib
    try:
        self_test()
    except CryptoUnavailable:
        _lib = None
        raise
    except Exception as e:  # noqa: BLE001 - any failure of the self-test means the library cannot be trusted
        _lib = None
        raise CryptoUnavailable(f"libcrypto failed its self-test: {e}") from None
    return lib


def generate_private_key() -> bytes:
    return os.urandom(KEY_LEN)


def _check_len(name: str, value: bytes, n: int) -> None:
    if not isinstance(value, (bytes, bytearray)) or len(value) != n:
        raise CryptoError(f"{name} must be {n} bytes")


def x25519_public(private: bytes) -> bytes:
    _check_len("private key", private, KEY_LEN)
    lib = load()
    key = lib.EVP_PKEY_new_raw_private_key(_NID_X25519, None, bytes(private), KEY_LEN)
    if not key:
        raise CryptoError("invalid private key")
    try:
        out, n = ctypes.create_string_buffer(KEY_LEN), ctypes.c_size_t(KEY_LEN)
        if lib.EVP_PKEY_get_raw_public_key(key, out, ctypes.byref(n)) != 1:
            raise CryptoError("could not derive the public key")
        return out.raw
    finally:
        lib.EVP_PKEY_free(key)


def x25519(private: bytes, peer_public: bytes) -> bytes:
    """The Diffie-Hellman shared secret; raises CryptoError for a low-order peer key (all-zero result)."""
    _check_len("private key", private, KEY_LEN)
    _check_len("public key", peer_public, KEY_LEN)
    lib = load()
    mine = lib.EVP_PKEY_new_raw_private_key(_NID_X25519, None, bytes(private), KEY_LEN)
    peer = lib.EVP_PKEY_new_raw_public_key(_NID_X25519, None, bytes(peer_public), KEY_LEN)
    ctx = lib.EVP_PKEY_CTX_new(mine, None) if mine else None
    try:
        if not (mine and peer and ctx) or lib.EVP_PKEY_derive_init(ctx) != 1 or lib.EVP_PKEY_derive_set_peer(ctx, peer) != 1:
            raise CryptoError("invalid key")
        out, n = ctypes.create_string_buffer(KEY_LEN), ctypes.c_size_t(KEY_LEN)
        if lib.EVP_PKEY_derive(ctx, out, ctypes.byref(n)) != 1:
            raise CryptoError("key agreement failed")
        return out.raw
    finally:
        if ctx:
            lib.EVP_PKEY_CTX_free(ctx)
        if mine:
            lib.EVP_PKEY_free(mine)
        if peer:
            lib.EVP_PKEY_free(peer)


class _Aead:
    """ChaCha20-Poly1305 cipher object, fetched once."""

    def __init__(self, lib):
        self.lib = lib
        self.cipher = lib.EVP_CIPHER_fetch(None, b"ChaCha20-Poly1305", None)
        if not self.cipher:
            raise CryptoUnavailable("libcrypto has no ChaCha20-Poly1305")


_aead = None


def _cipher():
    global _aead
    lib = load()
    if _aead is None or _aead.lib is not lib:
        _aead = _Aead(lib)
    return _aead


def aead_encrypt(key: bytes, nonce: bytes, ad: bytes, plaintext: bytes) -> bytes:
    """ChaCha20-Poly1305: returns ciphertext || 16-byte tag."""
    _check_len("key", key, KEY_LEN)
    _check_len("nonce", nonce, NONCE_LEN)
    a = _cipher()
    lib = a.lib
    ctx, n = lib.EVP_CIPHER_CTX_new(), ctypes.c_int(0)
    if not ctx:
        raise CryptoError("out of memory")
    try:
        if lib.EVP_EncryptInit_ex2(ctx, a.cipher, None, None, None) != 1 or lib.EVP_CIPHER_CTX_ctrl(ctx, _CTRL_SET_IVLEN, NONCE_LEN, None) != 1 \
                or lib.EVP_EncryptInit_ex2(ctx, None, bytes(key), bytes(nonce), None) != 1:
            raise CryptoError("encryption setup failed")
        if ad and lib.EVP_EncryptUpdate(ctx, None, ctypes.byref(n), bytes(ad), len(ad)) != 1:
            raise CryptoError("encryption failed")
        out = ctypes.create_string_buffer(len(plaintext) + TAG_LEN)
        if lib.EVP_EncryptUpdate(ctx, out, ctypes.byref(n), bytes(plaintext), len(plaintext)) != 1:
            raise CryptoError("encryption failed")
        written = n.value
        if lib.EVP_EncryptFinal_ex(ctx, out, ctypes.byref(n)) != 1:
            raise CryptoError("encryption failed")
        tag = ctypes.create_string_buffer(TAG_LEN)
        if lib.EVP_CIPHER_CTX_ctrl(ctx, _CTRL_GET_TAG, TAG_LEN, tag) != 1:
            raise CryptoError("no tag")
        return out.raw[:written] + tag.raw
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def aead_decrypt(key: bytes, nonce: bytes, ad: bytes, ciphertext: bytes) -> bytes:
    """Inverse of aead_encrypt; raises CryptoError (a ValueError) when the tag does not verify."""
    _check_len("key", key, KEY_LEN)
    _check_len("nonce", nonce, NONCE_LEN)
    if len(ciphertext) < TAG_LEN:
        raise CryptoError("ciphertext is shorter than the tag")
    a = _cipher()
    lib = a.lib
    body, tag = bytes(ciphertext[:-TAG_LEN]), bytes(ciphertext[-TAG_LEN:])
    ctx, n = lib.EVP_CIPHER_CTX_new(), ctypes.c_int(0)
    if not ctx:
        raise CryptoError("out of memory")
    try:
        if lib.EVP_DecryptInit_ex2(ctx, a.cipher, None, None, None) != 1 or lib.EVP_CIPHER_CTX_ctrl(ctx, _CTRL_SET_IVLEN, NONCE_LEN, None) != 1 \
                or lib.EVP_DecryptInit_ex2(ctx, None, bytes(key), bytes(nonce), None) != 1:
            raise CryptoError("decryption setup failed")
        if ad and lib.EVP_DecryptUpdate(ctx, None, ctypes.byref(n), bytes(ad), len(ad)) != 1:
            raise CryptoError("decryption failed")
        out = ctypes.create_string_buffer(len(body) + TAG_LEN)
        if lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(n), body, len(body)) != 1:
            raise CryptoError("decryption failed")
        written = n.value
        if lib.EVP_CIPHER_CTX_ctrl(ctx, _CTRL_SET_TAG, TAG_LEN, ctypes.create_string_buffer(tag, TAG_LEN)) != 1:
            raise CryptoError("decryption failed")
        if lib.EVP_DecryptFinal_ex(ctx, out, ctypes.byref(n)) != 1:
            raise CryptoError("authentication failed")
        return out.raw[:written]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-SHA256 (the Noise-style two/three output split lives in noise.py)."""
    if not 0 < length <= 255 * 32:
        raise CryptoError("invalid HKDF length")
    prk = hmac_sha256(salt or bytes(32), ikm)
    out, block = b"", b""
    for i in range(1, -(-length // 32) + 1):
        block = hmac_sha256(prk, block + info + bytes([i]))
        out += block
    return out[:length]


def self_test() -> None:
    """Known-answer tests (RFC 7748 6.1, RFC 8439 2.8.2, RFC 5869 A.1) and a tamper check; raises on any mismatch."""
    h = bytes.fromhex
    alice = h("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    bob_pub = h("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
    if x25519_public(alice) != h("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"):
        raise CryptoUnavailable("X25519 public key mismatch")
    if x25519(alice, bob_pub) != h("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"):
        raise CryptoUnavailable("X25519 shared secret mismatch")
    key = h("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
    nonce, ad = h("070000004041424344454647"), h("50515253c0c1c2c3c4c5c6c7")
    pt = b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it."
    ct = aead_encrypt(key, nonce, ad, pt)
    if ct[-TAG_LEN:] != h("1ae10b594f09e26a7e902ecbd0600691") or aead_decrypt(key, nonce, ad, ct) != pt:
        raise CryptoUnavailable("ChaCha20-Poly1305 mismatch")
    try:
        aead_decrypt(key, nonce, ad, ct[:-1] + bytes([ct[-1] ^ 1]))
    except CryptoError:
        pass
    else:
        raise CryptoUnavailable("a tampered tag was accepted")
    okm = hkdf(h("000102030405060708090a0b0c"), bytes([0x0b] * 22), h("f0f1f2f3f4f5f6f7f8f9"), 42)
    if okm != h("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"):
        raise CryptoUnavailable("HKDF mismatch")
