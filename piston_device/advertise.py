"""Keeps the mDNS advertisement of this device up to date (address changes, paired flag)."""
import asyncio
import logging
import socket
from typing import Callable

from .mdns import MdnsAdvertiser
from .proto import PROTOCOL_VERSION

log = logging.getLogger("decky-ha.advertise")

SERVICE_TYPE = "_piston._tcp.local."
CHECK_SECONDS = 15


def outbound_ip() -> str | None:
    """Address of the interface the default route uses; no packet is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            ip = sock.getsockname()[0]
            return None if ip.startswith("0.") else ip
    except OSError:
        return None


class Advertiser:
    """`info()` returns {device_id, name, paired}; `port` is the protocol server port.
    `factory` builds the low-level advertiser (replaced in tests)."""

    def __init__(self, info: Callable[[], dict], port: int, ip_func: Callable[[], str | None] | None = None,
                 factory=MdnsAdvertiser, check_seconds: float = CHECK_SECONDS):
        self._info, self.port, self._factory, self._check = info, port, factory, check_seconds
        self._ip = ip_func or (lambda: outbound_ip())
        self._adv = None
        self._ip_now: str | None = None
        self._txt: dict | None = None
        self._task: asyncio.Task | None = None

    @property
    def advertising(self) -> bool:
        """True while an advertisement is up on the current address."""
        return self._adv is not None

    def _txt_now(self) -> dict:
        i = self._info()
        return {"device_id": i["device_id"], "name": i["name"], "proto": PROTOCOL_VERSION, "paired": int(bool(i["paired"]))}

    async def sync(self) -> None:
        """Bring the advertisement in line with the current address and TXT values."""
        ip, txt = self._ip(), self._txt_now()
        if ip != self._ip_now:
            await self._stop()
            if ip is None:
                self._ip_now = None
                return
            adv = self._factory(SERVICE_TYPE, txt["device_id"], f"{txt['device_id']}.local.", self.port, ip, txt)
            try:
                await adv.start()
            except OSError as e:
                log.warning("mDNS start failed on %s: %s", ip, e)
                return
            self._adv, self._ip_now, self._txt = adv, ip, txt
            log.info("advertising %s on %s:%s", txt["device_id"], ip, self.port)
        elif self._adv is not None and txt != self._txt:
            await self._adv.refresh(**txt)
            self._txt = txt

    async def _stop(self) -> None:
        adv, self._adv, self._ip_now = self._adv, None, None
        if adv is not None:
            try:
                await adv.stop()
            except OSError as e:
                log.debug("mDNS stop failed: %s", e)

    async def restart(self) -> None:
        """Announce again from scratch (after a suspend the multicast membership is gone)."""
        await self._stop()
        await self.sync()

    async def _loop(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception:  # noqa: BLE001
                log.exception("advertise sync failed")
            await asyncio.sleep(self._check)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self._stop()
