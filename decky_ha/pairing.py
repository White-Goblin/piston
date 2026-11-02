"""Pairing state machine on the device: shows a code, runs the SRP exchange, stores the secret."""
import time
from typing import Callable

from .proto import code as code_mod, messages as m, pake

LOCKOUT_SECONDS = 60


class PairingManager:
    """`get_secret()` returns the stored secret (bytes, empty when unpaired); `store_secret(bytes)` saves or
    clears it; `on_change()` is called whenever what the panel shows changes (code, paired state)."""

    def __init__(self, get_secret: Callable[[], bytes], store_secret: Callable[[bytes], None],
                 on_change: Callable[[], None] = lambda: None, clock: Callable[[], float] = time.monotonic,
                 pairing_code: code_mod.PairingCode | None = None):
        self._get_secret, self._store_secret, self._on_change, self._clock = get_secret, store_secret, on_change, clock
        self.code = pairing_code or code_mod.PairingCode(clock=clock)
        self._srp: pake.SrpServer | None = None
        self._locked_at: float | None = None

    @property
    def paired(self) -> bool:
        return bool(self._get_secret())

    def refresh(self) -> None:
        """Make a fresh code when the current one is spent, expired, or its lockout is over."""
        if self.paired:
            return
        locked = self.code.attempts >= code_mod.MAX_ATTEMPTS
        if self.code.used or (self.code.expired and not locked):
            self._new_code()
        elif locked and self._locked_at is not None and self._clock() - self._locked_at >= LOCKOUT_SECONDS:
            self._new_code()

    def _new_code(self) -> None:
        self.code.new_code()
        self._locked_at = None
        self._srp = None
        self._on_change()

    def state(self) -> dict:
        """What the panel shows. The code is only present while unpaired."""
        self.refresh()
        if self.paired:
            return {"paired": True}
        left = max(0, int(self.code._expires - self._clock()))
        return {"paired": False, "code": self.code.code if self.code.valid else "", "expires_in": left,
                "attempts_left": self.code.attempts_left, "locked": self._locked_at is not None}

    async def handle(self, frame: dict) -> dict:
        """One `pair` frame from the integration; returns the reply frame or raises ProtocolError."""
        if self.paired:
            raise m.ProtocolError(m.UNAUTHORIZED, "already paired", {"reason": "already_paired"})
        self.refresh()
        step = frame.get("step")
        if step == "begin":
            if not self.code.valid:
                raise self._unavailable()
            self._srp = pake.SrpServer(self.code.code)
            return {"type": "pair", "step": "challenge", "salt": self._srp.salt.hex(), "B": pake.to_hex(self._srp.B)}
        if step == "proof":
            return self._proof(frame)
        raise m.ProtocolError(m.BAD_REQUEST, "unknown pairing step")

    def _unavailable(self) -> m.ProtocolError:
        if self._locked_at is not None:
            retry = max(0, int(LOCKOUT_SECONDS - (self._clock() - self._locked_at)))
            return m.ProtocolError(m.UNAUTHORIZED, "too many wrong codes", {"reason": "locked", "retry_in": retry})
        return m.ProtocolError(m.UNAUTHORIZED, "the code expired", {"reason": "expired"})

    def _proof(self, frame: dict) -> dict:
        srp, self._srp = self._srp, None
        if srp is None or not self.code.valid:
            raise self._unavailable() if not self.code.valid else m.ProtocolError(m.BAD_REQUEST, "begin first")
        try:
            a_pub = pake.from_hex(frame.get("A"))
            m1 = bytes.fromhex(frame.get("M1"))
            m2 = srp.verify(a_pub, m1)
        except (ValueError, TypeError, pake.PakeError):
            self.code.fail()
            if not self.code.valid:
                self._locked_at = self._clock()
            self._on_change()
            raise m.ProtocolError(m.UNAUTHORIZED, "wrong code",
                                  {"reason": "wrong_code", "attempts_left": self.code.attempts_left}) from None
        self.code.consume()
        self._store_secret(srp.secret)
        self._on_change()
        return {"type": "pair", "step": "done", "M2": m2.hex()}

    def unpair(self) -> None:
        """Forget the secret and start over with a new code (HA removed the device, or Reset pairing)."""
        self._store_secret(b"")
        self._new_code()


class PairingMixin:
    """Plugin side: owns the PairingManager and the callables/events the panel uses."""

    def _init_pairing(self) -> None:
        def store(secret: bytes) -> None:
            self._update_settings({"secret": secret.hex()})

        self.pairing = PairingManager(
            lambda: bytes.fromhex(self._load_settings()["secret"] or ""), store, self._pairing_changed)

    def _pairing_changed(self) -> None:
        loop = getattr(self, "loop", None)
        if loop is not None and loop.is_running():
            loop.create_task(self._emit_pairing())

    async def _emit_pairing(self) -> None:
        import decky
        await decky.emit("pairing", self.pairing.state())

    async def get_pairing_state(self) -> dict:
        return self.pairing.state()

    async def reset_pairing(self) -> dict:
        """Panel: forget the pairing; disconnect the integration and show a new code."""
        self.pairing.unpair()
        server = getattr(self, "server", None)
        if server is not None:
            await server.disconnect(4002, "pairing reset")
        return self.pairing.state()

    async def _cmd_unpair(self, data: dict) -> dict:
        """Command from the integration (device removed in Home Assistant)."""
        self.pairing.unpair()
        server = getattr(self, "server", None)
        if server is not None:
            # Let the reply go out first, then hang up: the old secret is gone
            self.loop.call_later(0.5, lambda: self.loop.create_task(server.disconnect(4002, "unpaired")))
        return {}
