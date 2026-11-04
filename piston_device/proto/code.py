"""Pairing code lifecycle: short, single use, expiring, limited wrong attempts (stdlib only)."""
import secrets
import time
from typing import Callable

DIGITS = 6
TTL_SECONDS = 300
MAX_ATTEMPTS = 5


class PairingCode:
    """One displayed code. `check()` is the only way to learn whether a guess matched."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, digits: int = DIGITS,
                 ttl: float = TTL_SECONDS, max_attempts: int = MAX_ATTEMPTS):
        self._clock, self._digits, self._ttl, self._max = clock, digits, ttl, max_attempts
        self.new_code()

    def new_code(self) -> str:
        """Replace the code (also the way to recover after expiry or too many attempts)."""
        self.code = "".join(secrets.choice("0123456789") for _ in range(self._digits))
        self._expires = self._clock() + self._ttl
        self.attempts = 0
        self.used = False
        return self.code

    @property
    def expired(self) -> bool:
        return self._clock() >= self._expires

    @property
    def valid(self) -> bool:
        return not self.used and not self.expired and self.attempts < self._max

    def fail(self) -> None:
        """Count a wrong attempt; the code is invalid once the limit is reached."""
        self.attempts += 1

    def consume(self) -> None:
        """Mark the code used after a successful pairing."""
        self.used = True

    @property
    def attempts_left(self) -> int:
        return max(0, self._max - self.attempts)
