import os
import struct
import unittest

from . import fake  # noqa: F401  (installs the decky stub)
from piston_device.sendspin.artwork import ArtworkAssembler, MAX_IMAGE_BYTES, MAX_PART_BYTES

JPEG = b"\xff\xd8\xff\xe0" + os.urandom(150000)
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(500)


def announce(size, ts=0):
    return bytes([8, 2]) + struct.pack(">qI", ts, size)


def part(data):
    return bytes([8, 0]) + data


def parts(data, n=MAX_PART_BYTES):
    return [part(data[i:i + n]) for i in range(0, len(data), n)]


class Clock:
    def __init__(self):
        self.t = 1000

    def now(self):
        return self.t


class AssemblerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.a = ArtworkAssembler(lambda ts: ts, self.clock.now)

    def send(self, data, ts=0):
        events = self.a.feed(announce(len(data), ts))
        for p in parts(data):
            events += self.a.feed(p)
        return events

    def test_a_multi_part_jpeg_assembles_bit_exactly_and_is_shown_at_once_when_due(self):
        events = self.send(JPEG, ts=500)
        self.assertIn("changed", events)
        cover = self.a.current()
        self.assertEqual((cover.data, cover.content_type), (JPEG, "image/jpeg"))
        self.assertEqual(self.a.lookup(cover.id), cover)
        self.assertIsNone(self.a.lookup("other"))

    def test_the_id_changes_with_each_image_even_for_the_same_bytes(self):
        self.send(JPEG)
        first = self.a.current().id
        self.send(PNG)
        second = self.a.current()
        self.assertNotEqual(first, second.id)
        self.assertEqual(second.content_type, "image/png")
        self.send(PNG)
        self.assertNotEqual(self.a.current().id, second.id)
        self.assertIsNone(self.a.lookup(first))

    def test_an_unknown_image_type_gets_a_generic_content_type(self):
        self.send(b"hello world")
        self.assertEqual(self.a.current().content_type, "application/octet-stream")

    def test_an_image_waits_for_its_timestamp(self):
        self.send(JPEG, ts=5000)
        self.assertIsNone(self.a.current())
        self.assertTrue(self.a.waiting())
        self.clock.t = 4999
        self.assertFalse(self.a.poll())
        self.clock.t = 5000
        self.assertTrue(self.a.poll())
        self.assertEqual(self.a.current().data, JPEG)
        self.assertFalse(self.a.waiting())

    def test_a_waiting_image_does_not_replace_the_current_one_until_due(self):
        self.send(PNG)
        old = self.a.current()
        self.send(JPEG, ts=9000)
        self.assertEqual(self.a.current(), old)
        self.clock.t = 9000
        self.a.poll()
        self.assertEqual(self.a.current().data, JPEG)

    def test_clear_removes_the_image_and_can_be_scheduled(self):
        self.send(JPEG)
        self.assertIn("clear", self.a.feed(announce(0, 0)))
        self.assertIsNone(self.a.current())
        self.send(JPEG)
        self.a.feed(announce(0, 7000))
        self.assertIsNotNone(self.a.current())
        self.clock.t = 7000
        self.assertTrue(self.a.poll())
        self.assertIsNone(self.a.current())

    def test_extra_bytes_orphan_parts_and_oversize_discard_the_transfer_and_keep_the_previous_image(self):
        self.send(PNG)
        old = self.a.current()
        self.a.feed(announce(10))
        self.assertIn("discard", self.a.feed(part(b"x" * 11)))
        self.assertIn("discard", self.a.feed(part(b"x")))          # now an orphan
        self.assertIn("discard", self.a.feed(announce(MAX_IMAGE_BYTES + 1)))
        self.a.feed(announce(MAX_PART_BYTES + 5))
        self.assertIn("discard", self.a.feed(part(b"x" * (MAX_PART_BYTES + 1))))
        self.assertEqual(self.a.discarded, 4)
        self.assertEqual(self.a.current(), old)

    def test_a_cancel_and_a_new_announce_drop_the_transfer_in_flight(self):
        self.send(PNG)
        old = self.a.current()
        self.a.feed(announce(100))
        self.a.feed(part(b"a" * 50))
        self.assertEqual(self.a.feed(bytes([8, 1])), ["cancel"])
        self.assertIn("discard", self.a.feed(part(b"a" * 50)))
        self.a.feed(announce(100))
        self.a.feed(part(b"a" * 50))
        self.a.feed(announce(4))                                    # restarts
        self.a.feed(part(b"\xff\xd8\xff\xaa"))
        self.assertEqual(self.a.current().data, b"\xff\xd8\xff\xaa")
        self.assertNotEqual(self.a.current(), old)

    def test_malformed_messages_are_discarded(self):
        for raw in (b"", b"\x08", bytes([8, 2, 0, 0]), bytes([9, 0, 1])):
            self.assertIn("discard", self.a.feed(raw))

    def test_an_unusable_clock_shows_the_image_at_once(self):
        for convert in (lambda ts: None, lambda ts: 1 / 0):
            a = ArtworkAssembler(convert, self.clock.now)
            a.feed(announce(4, 10**15))
            a.feed(part(b"\xff\xd8\xff\xaa"))
            self.assertIsNotNone(a.current())

    def test_reset_forgets_everything(self):
        self.send(JPEG, ts=10**9)
        self.a.reset()
        self.assertFalse(self.a.waiting())
        self.assertIsNone(self.a.current())


if __name__ == "__main__":
    unittest.main()
