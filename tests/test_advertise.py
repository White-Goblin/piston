import struct
import unittest

from decky_ha import mdns
from decky_ha.advertise import Advertiser


def question(name: str, qtype: int) -> bytes:
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + mdns._name(name) + struct.pack(">HH", qtype, 1)


def parse_records(packet: bytes) -> dict:
    """Decode answers into {type: rdata-bytes}; enough to check the service is described correctly."""
    _id, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", packet[:12])
    assert flags & 0x8400 == 0x8400 and qd == 0
    off, out = 12, {}
    for _ in range(an):
        name, off = mdns._read_name(packet, off)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", packet[off:off + 10])
        off += 10
        out[rtype] = (name, packet[off:off + rdlen])
        off += rdlen
    return out


class MdnsPacketTests(unittest.TestCase):
    def setUp(self):
        self.adv = mdns.MdnsAdvertiser("_deckyha._tcp.local.", "abc123", "abc123.local.", 47830, "192.168.1.5",
                                       {"device_id": "abc123", "paired": 0})

    def test_answers_describe_the_service(self):
        rec = parse_records(self.adv._packet())
        self.assertEqual(rec[mdns.T_PTR][0], "_deckyha._tcp.local.")
        self.assertEqual(mdns._read_name(rec[mdns.T_PTR][1], 0)[0], "abc123._deckyha._tcp.local.")
        port = struct.unpack(">HHH", rec[mdns.T_SRV][1][:6])[2]
        self.assertEqual(port, 47830)
        self.assertEqual(rec[mdns.T_A][1], bytes([192, 168, 1, 5]))
        self.assertIn(b"device_id=abc123", rec[mdns.T_TXT][1])

    def test_goodbye_has_zero_ttl(self):
        packet = self.adv._packet(0)
        off = 12
        _name, off = mdns._read_name(packet, off)
        self.assertEqual(struct.unpack(">HHIH", packet[off:off + 10])[2], 0)

    def test_wants_only_our_names(self):
        self.assertTrue(self.adv.wants(question("_deckyha._tcp.local.", mdns.T_PTR)))
        self.assertTrue(self.adv.wants(question("abc123.local.", mdns.T_A)))
        self.assertFalse(self.adv.wants(question("_other._tcp.local.", mdns.T_PTR)))
        self.assertFalse(self.adv.wants(b"junk"))
        self.assertFalse(self.adv.wants(self.adv._packet()))  # a response, not a query

    def test_txt_update_changes_packet(self):
        self.assertIn(b"paired=1", parse_records(self.adv.update_txt(paired=1))[mdns.T_TXT][1])


class FakeLow:
    instances = []

    def __init__(self, stype, instance, host, port, ip, txt):
        self.ip, self.txt, self.events = ip, dict(txt), []
        FakeLow.instances.append(self)

    async def start(self):
        self.events.append("start")

    async def stop(self):
        self.events.append("stop")

    async def refresh(self, **txt):
        self.txt = txt
        self.events.append("refresh")


class AdvertiserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeLow.instances = []
        self.info = {"device_id": "abc123", "name": "box", "paired": False}
        self.ip = "10.0.0.2"
        self.adv = Advertiser(lambda: self.info, 47830, lambda: self.ip, FakeLow)

    async def test_starts_with_unpaired_txt(self):
        await self.adv.sync()
        self.assertEqual(len(FakeLow.instances), 1)
        self.assertEqual(FakeLow.instances[0].txt["paired"], 0)

    async def test_paired_flag_change_refreshes_without_restart(self):
        await self.adv.sync()
        self.info["paired"] = True
        await self.adv.sync()
        self.assertEqual(len(FakeLow.instances), 1)
        self.assertEqual(FakeLow.instances[0].txt["paired"], 1)
        self.assertEqual(FakeLow.instances[0].events, ["start", "refresh"])

    async def test_ip_change_restarts_on_the_new_address(self):
        await self.adv.sync()
        self.ip = "10.0.0.9"
        await self.adv.sync()
        self.assertEqual([i.ip for i in FakeLow.instances], ["10.0.0.2", "10.0.0.9"])
        self.assertEqual(FakeLow.instances[0].events, ["start", "stop"])

    async def test_no_network_then_network(self):
        self.ip = None
        await self.adv.sync()
        self.assertEqual(FakeLow.instances, [])
        self.ip = "10.0.0.2"
        await self.adv.sync()
        self.assertEqual(len(FakeLow.instances), 1)

    async def test_stop_says_goodbye(self):
        await self.adv.sync()
        await self.adv.stop()
        self.assertEqual(FakeLow.instances[0].events, ["start", "stop"])


class RobustStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_goodbye_does_not_wedge_the_advertiser(self):
        class Boom(FakeLow):
            async def stop(self):
                raise OSError("network unreachable")

        FakeLow.instances = []
        ip = {"v": "10.0.0.2"}
        adv = Advertiser(lambda: {"device_id": "abc123", "name": "box", "paired": False}, 47830, lambda: ip["v"], Boom)
        await adv.sync()
        ip["v"] = "10.0.0.9"
        await adv.sync()  # must not raise, and must start on the new address
        self.assertEqual([i.ip for i in FakeLow.instances], ["10.0.0.2", "10.0.0.9"])

    async def test_restart_announces_again(self):
        FakeLow.instances = []
        adv = Advertiser(lambda: {"device_id": "abc123", "name": "box", "paired": False}, 47830, lambda: "10.0.0.2", FakeLow)
        await adv.sync()
        await adv.restart()
        self.assertEqual(len(FakeLow.instances), 2)
