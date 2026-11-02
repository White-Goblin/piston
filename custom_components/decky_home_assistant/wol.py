"""Wake on LAN: the integration sends the magic packet, since the plugin is not running while the device sleeps."""
from __future__ import annotations

import asyncio
import re
import socket

PORTS = (9, 7)
MAC_RE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.IGNORECASE)


def magic_packet(mac: str) -> bytes:
    """6 x 0xFF followed by the MAC 16 times."""
    if not MAC_RE.match(mac):
        raise ValueError(f"not a MAC address: {mac}")
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    return b"\xff" * 6 + raw * 16


def _send(mac: str, broadcasts: tuple) -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for address in broadcasts:
            for port in PORTS:
                sock.sendto(packet, (address, port))


async def send_magic_packet(mac: str, broadcast: str = "255.255.255.255") -> None:
    """Send to the device's subnet broadcast and the limited broadcast, on ports 9 and 7."""
    addresses = tuple(dict.fromkeys((broadcast or "255.255.255.255", "255.255.255.255")))
    await asyncio.get_running_loop().run_in_executor(None, _send, mac, addresses)
