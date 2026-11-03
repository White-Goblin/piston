import json
import unittest

from decky_ha.proto import MIN_PROTOCOL_VERSION, PROTOCOL_VERSION, messages as m


class ParseTests(unittest.TestCase):
    def test_roundtrip(self):
        for frame in (m.hello("abc"), m.cmd(1, "launch", {"appid": "1"}), m.result_ok(1), m.result_error(2, m.BUSY),
                      m.state("apps", {"x": 1}, "boot1", 1), m.event("toast_click", {"id": "a"})):
            self.assertEqual(m.parse(m.encode(frame))["type"], frame["type"])

    def test_defaults_data(self):
        self.assertEqual(m.parse('{"type":"cmd","id":1,"name":"quit"}')["data"], {})

    def test_rejects_malformed(self):
        bad = ["", "not json", "[]", '{"type":"nope"}', '{"type":"cmd","id":-1,"name":"x"}',
               '{"type":"cmd","id":true,"name":"x"}', '{"type":"cmd","id":1,"name":""}',
               '{"type":"cmd","id":1,"name":"x","data":[]}', '{"type":"result","id":1,"ok":false}',
               '{"type":"hello","proto":"1","min_proto":1,"device_id":"a"}', '{"type":"hello","proto":1,"min_proto":1}',
               '{"type":"state","name":"a'+"b"*70+'"}']
        for text in bad:
            with self.assertRaises(m.ProtocolError, msg=text) as cm:
                m.parse(text)
            self.assertEqual(cm.exception.code, m.BAD_REQUEST)

    def test_rejects_invalid_utf8_and_oversize(self):
        with self.assertRaises(m.ProtocolError):
            m.parse(b"\xff\xfe")
        with self.assertRaises(m.ProtocolError):
            m.parse(json.dumps({"type": "event", "name": "e", "data": {"x": "a" * m.MAX_FRAME_BYTES}}))
        with self.assertRaises(m.ProtocolError):
            m.encode(m.event("e", {"x": "a" * m.MAX_FRAME_BYTES}))


class StateSequencingTests(unittest.TestCase):
    def frame(self, **override):
        frame = {"type": "state", "name": "apps", "data": {"x": 1}, "boot_id": "ab12", "seq": 1}
        frame.update(override)
        return {k: v for k, v in frame.items() if v is not None or k not in override}

    def test_builder_and_parse_roundtrip(self):
        parsed = m.parse(m.encode(m.state("apps", {"x": 1}, "ab12", 7)))
        self.assertEqual((parsed["boot_id"], parsed["seq"], parsed["data"]), ("ab12", 7, {"x": 1}))

    def test_accepts_limits(self):
        self.assertEqual(m.parse(json.dumps(self.frame(seq=1)))["seq"], 1)
        self.assertEqual(m.parse(json.dumps(self.frame(boot_id="b" * m.MAX_BOOT_ID_LEN)))["boot_id"], "b" * 32)

    def test_rejects_missing_or_malformed_boot_id_and_seq(self):
        cases = [dict(boot_id=""), dict(boot_id=5), dict(boot_id="b" * (m.MAX_BOOT_ID_LEN + 1)), dict(boot_id=None),
                 dict(seq=0), dict(seq=-1), dict(seq="1"), dict(seq=True), dict(seq=1.5), dict(seq=None)]
        for override in cases:
            frame = self.frame(**override)
            with self.assertRaises(m.ProtocolError, msg=str(override)) as cm:
                m.parse(json.dumps(frame))
            self.assertEqual(cm.exception.code, m.BAD_REQUEST)

    def test_events_do_not_need_sequencing(self):
        self.assertEqual(m.parse('{"type":"event","name":"resumed"}')["name"], "resumed")


class NegotiateTests(unittest.TestCase):
    def test_same_and_previous_accepted(self):
        self.assertEqual(m.negotiate(2, 1, 2, 1), 2)
        self.assertEqual(m.negotiate(2, 1, 1, 1), 1)
        self.assertEqual(m.negotiate(1, 1, 2, 1), 1)

    def test_version_two_accepts_only_itself(self):
        self.assertEqual((PROTOCOL_VERSION, MIN_PROTOCOL_VERSION), (2, 2))
        self.assertEqual(m.negotiate(2, 2, 2, 2), 2)
        with self.assertRaises(m.ProtocolError) as cm:
            m.negotiate(2, 2, 1, 1)  # a v1 peer
        self.assertEqual((cm.exception.code, cm.exception.data["peer"]), (m.VERSION_SKEW, "old"))
        with self.assertRaises(m.ProtocolError) as cm:
            m.negotiate(2, 2, 3, 3)  # a newer peer that dropped v2
        self.assertEqual(cm.exception.data["peer"], "new")

    def test_skew(self):
        with self.assertRaises(m.ProtocolError) as cm:
            m.negotiate(3, 2, 1, 1)
        self.assertEqual((cm.exception.code, cm.exception.data["peer"]), (m.VERSION_SKEW, "old"))
        with self.assertRaises(m.ProtocolError) as cm:
            m.negotiate(1, 1, 3, 2)
        self.assertEqual(cm.exception.data["peer"], "new")
