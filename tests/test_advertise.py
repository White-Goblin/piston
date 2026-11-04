import socket
import struct
import unittest
from unittest import mock

from piston_device import mdns
from piston_device.advertise import Advertiser


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
        self.adv = mdns.MdnsAdvertiser("_piston._tcp.local.", "abc123", "abc123.local.", 47830, "192.168.1.5",
                                       {"device_id": "abc123", "paired": 0})

    def test_answers_describe_the_service(self):
        rec = parse_records(self.adv._packet())
        self.assertEqual(rec[mdns.T_PTR][0], "_piston._tcp.local.")
        self.assertEqual(mdns._read_name(rec[mdns.T_PTR][1], 0)[0], "abc123._piston._tcp.local.")
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
        self.assertTrue(self.adv.wants(question("_piston._tcp.local.", mdns.T_PTR)))
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


class FailedStartTests(unittest.IsolatedAsyncioTestCase):
    """After a resume the first start fails with 'Network is unreachable'; nothing may be left behind."""

    def adv(self):
        return mdns.MdnsAdvertiser("_piston._tcp.local.", "abc123", "abc123.local.", 47830, "192.168.1.5", {"device_id": "abc123"})

    def test_a_failed_open_closes_the_socket(self):
        closed = []

        class Sock:
            def __init__(self, *args):
                pass

            def setsockopt(self, level, option, value):
                if option == socket.IP_ADD_MEMBERSHIP:
                    raise OSError(101, "Network is unreachable")

            def bind(self, address):
                pass

            def close(self):
                closed.append(1)

        with mock.patch.object(mdns.socket, "socket", Sock):
            with self.assertRaises(OSError):
                self.adv()._open()
        self.assertEqual(closed, [1])

    async def test_a_failed_announcement_closes_the_socket(self):
        closed = []

        class Sock:
            def close(self):
                closed.append(1)

        adv = self.adv()

        async def fail(packet, times=2):
            raise OSError(101, "Network is unreachable")

        adv._open = lambda: Sock()
        adv._announce = fail
        with self.assertRaises(OSError):
            await adv.start()
        self.assertEqual((closed, adv.sock), ([1], None))


class RobustStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_advertising_follows_the_state_and_a_failed_start_is_retried(self):
        FakeLow.instances = []
        attempts = []

        class Flaky(FakeLow):
            async def start(self):
                attempts.append(1)
                if len(attempts) == 1:
                    raise OSError(101, "Network is unreachable")

        ip = {"v": "10.0.0.2"}
        adv = Advertiser(lambda: {"device_id": "abc123", "name": "box", "paired": False}, 47830, lambda: ip["v"], Flaky)
        self.assertFalse(adv.advertising)
        await adv.sync()
        self.assertFalse(adv.advertising)          # the first start failed
        await adv.restart()
        self.assertTrue(adv.advertising)           # and a restart after the failure works
        ip["v"] = None
        await adv.sync()
        self.assertFalse(adv.advertising)

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


class SendspinAdvertiserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeLow.instances = []
        self.calls = []

        class Recording(FakeLow):
            def __init__(inner, stype, instance, host, port, ip, txt):
                super().__init__(stype, instance, host, port, ip, txt)
                inner.args = (stype, instance, host, port, ip)
                outer_calls.append(inner)
        outer_calls = self.calls
        self.info = {"device_id": "abc123", "client_id": "C" * 43, "name": "Living room"}
        self.ip = "10.0.0.2"
        from piston_device.advertise import sendspin_advertiser
        self.adv = sendspin_advertiser(lambda: self.info, 8928, lambda: self.ip, Recording)

    async def test_it_announces_the_sendspin_service_with_the_client_id_and_the_required_txt(self):
        await self.adv.sync()
        low = self.calls[0]
        self.assertEqual(low.args, ("_sendspin._tcp.local.", "C" * 43, "abc123.local.", 8928, "10.0.0.2"))
        self.assertEqual(low.txt, {"path": "/sendspin", "name": "Living room"})

    async def test_a_name_change_refreshes_without_restart(self):
        await self.adv.sync()
        self.info["name"] = "Bedroom"
        await self.adv.sync()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].txt["name"], "Bedroom")
        self.assertEqual(self.calls[0].events, ["start", "refresh"])

    async def test_a_port_change_announces_again_on_the_new_port(self):
        await self.adv.sync()
        self.adv.port = 8929
        await self.adv.sync()
        self.assertEqual([c.args[3] for c in self.calls], [8928, 8929])
        self.assertEqual(self.calls[0].events, ["start", "stop"])
        await self.adv.sync()                                                    # unchanged: nothing more happens
        self.assertEqual(len(self.calls), 2)

    async def test_an_address_change_restarts_and_stop_withdraws_the_record(self):
        await self.adv.sync()
        self.ip = "10.0.0.9"
        await self.adv.sync()
        self.assertEqual([c.args[4] for c in self.calls], ["10.0.0.2", "10.0.0.9"])
        await self.adv.stop()
        self.assertEqual(self.calls[1].events, ["start", "stop"])
        self.assertFalse(self.adv.advertising)

    async def test_it_runs_independently_of_the_piston_advertiser(self):
        piston = Advertiser(lambda: {"device_id": "abc123", "name": "box", "paired": False}, 47830, lambda: self.ip, FakeLow)
        await piston.sync()
        await self.adv.sync()
        await self.adv.stop()
        self.assertTrue(piston.advertising)
        self.assertEqual(FakeLow.instances[0].events, ["start"])                  # Piston's record is untouched

    async def test_the_real_record_has_the_service_type_instance_and_txt(self):
        low = mdns.MdnsAdvertiser("_sendspin._tcp.local.", "C" * 43, "abc123.local.", 8928, "192.168.1.5", {"path": "/sendspin", "name": "Box"})
        packet = low._packet()
        for needle in (b"_sendspin", b"path=/sendspin", b"name=Box", b"C" * 43):
            self.assertIn(needle, packet)
        self.assertLess(len("C" * 43), 64)                                       # fits in one DNS label
