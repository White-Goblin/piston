"""Wait until the network is usable again after a resume.

After a wake the wired link is cycled and the address comes back only with the DHCP lease (9-14 s on the dev
device, see swe/notes/push-state/baseline.md). Work that needs the network (re-advertising on mDNS, reading the
interface and Wake on LAN facts, the first live push to Home Assistant) should wait for it instead of failing
against a down link and retrying much later.
"""
import asyncio
import time
from typing import Awaitable, Callable

from .advertise import outbound_ip

NETWORK_WAIT_CAP_SECONDS = 60.0
POLL_SECONDS = 0.25


async def wait_for_network(cap: float = NETWORK_WAIT_CAP_SECONDS, poll: float = POLL_SECONDS,
                           ready: Callable[[], object] = outbound_ip,
                           sleep: Callable[[float], Awaitable] = asyncio.sleep,
                           clock: Callable[[], float] = time.monotonic) -> bool:
    """True as soon as a default route and an address exist (checked without sending a packet); False after `cap` seconds.

    Returns at once when the network is already up. Any error while checking counts as "not ready yet"."""
    deadline = clock() + cap
    while True:
        try:
            if ready():
                return True
        except Exception:  # noqa: BLE001
            pass
        if clock() >= deadline:
            return False
        await sleep(poll)
