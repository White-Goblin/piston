"""Minimal mDNS service advertiser (stdlib only): answers PTR/SRV/TXT/A queries for one service.

Checked against avahi on a real device. IPv4, one interface."""
import asyncio
import socket
import struct

MCAST = "224.0.0.251"
PORT = 5353
TTL = 120
T_A, T_PTR, T_TXT, T_SRV, T_ANY = 1, 12, 16, 33, 255
CLASS_IN, FLUSH = 1, 0x8000


def _name(n: str) -> bytes:
    out = b""
    for label in n.rstrip(".").split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def _read_name(data: bytes, off: int):
    labels, jumped, end = [], False, off
    while True:
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = ((n & 0x3F) << 8) | data[off + 1]
            if not jumped:
                end = off + 2
            jumped, off = True, ptr
            continue
        labels.append(data[off + 1:off + 1 + n].decode(errors="replace"))
        off += 1 + n
    return ".".join(labels).lower() + ".", (end if jumped else off)


def _rr(name: str, rtype: int, rdata: bytes, ttl: int, flush: bool = True) -> bytes:
    cls = CLASS_IN | (FLUSH if flush else 0)
    return _name(name) + struct.pack(">HHIH", rtype, cls, ttl, len(rdata)) + rdata


class MdnsAdvertiser:
    def __init__(self, service_type, instance, host, port, ip, txt):
        self.stype = service_type.rstrip(".") + "."
        self.inst = f"{instance}.{self.stype}"
        self.host = host.rstrip(".") + "."
        self.port, self.ip, self.txt = port, ip, dict(txt)
        self.sock = None
        self._task = None

    def _records(self, ttl: int = TTL):
        txt = b"".join(bytes([len(kv)]) + kv for kv in (f"{k}={v}".encode() for k, v in self.txt.items())) or b"\0"
        srv = struct.pack(">HHH", 0, 0, self.port) + _name(self.host)
        return {
            "ptr": _rr(self.stype, T_PTR, _name(self.inst), ttl, flush=False),
            "srv": _rr(self.inst, T_SRV, srv, ttl),
            "txt": _rr(self.inst, T_TXT, txt, ttl),
            "a": _rr(self.host, T_A, socket.inet_aton(self.ip), ttl),
        }

    def _packet(self, ttl: int = TTL) -> bytes:
        recs = self._records(ttl)
        body = b"".join(recs.values())
        return struct.pack(">HHHHHH", 0, 0x8400, 0, len(recs), 0, 0) + body

    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("", PORT))
            iface = socket.inet_aton(self.ip)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(MCAST) + iface)  # fails while the link is down
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            s.setblocking(False)
        except BaseException:
            s.close()  # a failed start must not leave a socket bound to the mDNS port behind
            raise
        return s

    def wants(self, data: bytes) -> bool:
        """True when a query packet asks for something this service answers."""
        try:
            flags, qd = struct.unpack(">2xHH6x", data[:12])[0:2]
            if flags & 0x8000 or not qd:
                return False
            off = 12
            for _ in range(qd):
                name, off = _read_name(data, off)
                qtype = struct.unpack(">H", data[off:off + 2])[0]
                off += 4
                if name in (self.stype, self.inst, self.host) and qtype in (T_PTR, T_SRV, T_TXT, T_A, T_ANY):
                    return True
        except (struct.error, IndexError, UnicodeDecodeError):
            return False
        return False

    def update_txt(self, **txt) -> bytes:
        self.txt.update({k: str(v) for k, v in txt.items()})
        return self._packet()

    async def _announce(self, packet: bytes, times: int = 2):
        for _ in range(times):
            self.sock.sendto(packet, (MCAST, PORT))
            await asyncio.sleep(1)

    async def _serve(self):
        loop = asyncio.get_running_loop()
        while True:
            data, _addr = await loop.sock_recvfrom(self.sock, 9000)
            if self.wants(data):
                self.sock.sendto(self._packet(), (MCAST, PORT))

    async def start(self):
        self.sock = self._open()
        try:
            await self._announce(self._packet(), 2)
        except BaseException:
            self.sock.close()
            self.sock = None
            raise
        self._task = asyncio.create_task(self._serve())

    async def refresh(self, **txt):
        """Re-announce with changed TXT values (e.g. paired)."""
        self.sock.sendto(self.update_txt(**txt), (MCAST, PORT))

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self.sock:
            try:
                self.sock.sendto(self._packet(0), (MCAST, PORT))  # goodbye; may fail when the network is gone
            except OSError:
                pass
            finally:
                self.sock.close()
                self.sock = None
