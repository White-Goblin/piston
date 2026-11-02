import json
import unittest

from decky_ha.proto import messages as m


class ParseTests(unittest.TestCase):
    def test_roundtrip(self):
        for frame in (m.hello("abc"), m.cmd(1, "launch", {"appid": "1"}), m.result_ok(1), m.result_error(2, m.BUSY),
                      m.state("apps", {"x": 1}), m.event("toast_click", {"id": "a"})):
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


class NegotiateTests(unittest.TestCase):
    def test_same_and_previous_accepted(self):
        self.assertEqual(m.negotiate(2, 1, 2, 1), 2)
        self.assertEqual(m.negotiate(2, 1, 1, 1), 1)
        self.assertEqual(m.negotiate(1, 1, 2, 1), 1)

    def test_skew(self):
        with self.assertRaises(m.ProtocolError) as cm:
            m.negotiate(3, 2, 1, 1)
        self.assertEqual((cm.exception.code, cm.exception.data["peer"]), (m.VERSION_SKEW, "old"))
        with self.assertRaises(m.ProtocolError) as cm:
            m.negotiate(1, 1, 3, 2)
        self.assertEqual(cm.exception.data["peer"], "new")
