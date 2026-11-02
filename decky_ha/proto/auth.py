"""Per-connection mutual authentication with the stored per-device secret (stdlib only).

Device sends a nonce, HA answers with its own nonce and a proof; the device verifies and sends its
proof back. Proofs are role-labelled so one cannot be reflected as the other.
"""
import hashlib
import hmac
import secrets

ROLE_HA = b"ha"
ROLE_DEVICE = b"dev"
NONCE_BYTES = 16


def make_challenge() -> str:
    """A fresh nonce as lowercase hex."""
    return secrets.token_hex(NONCE_BYTES)


def prove(secret: bytes, nonce_device: str, nonce_ha: str, role: bytes) -> str:
    """HMAC-SHA256 over role | device nonce | HA nonce, as hex."""
    if role not in (ROLE_HA, ROLE_DEVICE):
        raise ValueError("unknown role")
    msg = role + b"|" + nonce_device.encode() + b"|" + nonce_ha.encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify(secret: bytes, nonce_device: str, nonce_ha: str, role: bytes, proof) -> bool:
    if not isinstance(proof, str):
        return False
    return hmac.compare_digest(prove(secret, nonce_device, nonce_ha, role), proof)
