import os
import struct
import tempfile
import unittest
from unittest import mock

from piston_device import uinput as u
from piston_device.proto import messages as m

EVENT = struct.Struct("llHHi")


def events_of(data: bytes):
    return [EVENT.unpack_from(data, i)[2:] for i in range(0, len(data), EVENT.size)]


class KeyMapTests(unittest.TestCase):
    def test_letters_digits_and_punctuation_map_to_a_key(self):
        self.assertEqual(u.char_to_key("a"), (30, False))
        self.assertEqual(u.char_to_key("q"), (16, False))
        self.assertEqual(u.char_to_key("m"), (50, False))
        self.assertEqual(u.char_to_key("1"), (2, False))
        self.assertEqual(u.char_to_key("0"), (11, False))
        self.assertEqual(u.char_to_key(" "), (57, False))
        self.assertEqual(u.char_to_key("."), (52, False))

    def test_capitals_and_shifted_symbols_need_shift(self):
        self.assertEqual(u.char_to_key("A"), (30, True))
        self.assertEqual(u.char_to_key("Z"), (44, True))
        self.assertEqual(u.char_to_key("!"), (2, True))
        self.assertEqual(u.char_to_key("?"), (53, True))
        self.assertEqual(u.char_to_key("_"), (12, True))

    def test_newline_and_tab_are_enter_and_tab(self):
        self.assertEqual(u.char_to_key("\n"), (u.KEY["Enter"], False))
        self.assertEqual(u.char_to_key("\t"), (u.KEY["Tab"], False))

    def test_unmappable_characters_return_none(self):
        for ch in ("é", "€", "\x00", "\x07", "日", "😀"):
            self.assertIsNone(u.char_to_key(ch), ch)

    def test_every_printable_ascii_character_maps(self):
        for code in range(32, 127):
            self.assertIsNotNone(u.char_to_key(chr(code)), chr(code))

    def test_every_protocol_key_name_has_a_code(self):
        for name in m.INPUT_KEYS:
            self.assertIn(name, u.KEY)


class FakeUinput(unittest.TestCase):
    """A temp file stands in for /dev/uinput; the ioctls are recorded instead of sent."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.addCleanup(os.unlink, self.tmp.name)
        self.ioctls = []
        patcher = mock.patch.object(u.fcntl, "ioctl", lambda fd, req, arg=0: self.ioctls.append((req, arg)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def written(self) -> list:
        with open(self.tmp.name, "rb") as f:
            return events_of(f.read())


class PointerTests(FakeUinput):
    def test_setup_declares_the_buttons_and_axes_and_creates_the_device(self):
        p = u.Pointer(self.tmp.name)
        requests = [r for r, _ in self.ioctls]
        self.assertEqual(requests[0], u.UI_SET_EVBIT)
        self.assertEqual([a for r, a in self.ioctls if r == u.UI_SET_KEYBIT], [u.BTN_LEFT, u.BTN_RIGHT, u.BTN_MIDDLE])
        self.assertEqual(sorted(a for r, a in self.ioctls if r == u.UI_SET_RELBIT), sorted([u.REL_X, u.REL_Y, u.REL_WHEEL, u.REL_HWHEEL]))
        setup = [a for r, a in self.ioctls if r == u.UI_DEV_SETUP][0]
        self.assertTrue(setup.startswith(struct.pack("HHHH", u.BUS_VIRTUAL, 0x1209, 0xDE01, 1)))
        self.assertIn(b"Piston Pointer", setup)
        self.assertEqual(requests[-1], u.UI_DEV_CREATE)
        p.close()
        self.assertEqual(self.ioctls[-1][0], u.UI_DEV_DESTROY)

    def test_move_button_and_scroll_events_have_the_expected_layout(self):
        p = u.Pointer(self.tmp.name)
        p.move(5, -3)
        p.move(0, 0)                      # nothing to say: nothing written
        p.button(u.BTN_RIGHT, True)
        p.button(u.BTN_RIGHT, False)
        p.scroll(2, -1)
        p.close()
        self.assertEqual(self.written(), [
            (u.EV_REL, u.REL_X, 5), (u.EV_REL, u.REL_Y, -3), (u.EV_SYN, 0, 0),
            (u.EV_KEY, u.BTN_RIGHT, 1), (u.EV_SYN, 0, 0), (u.EV_KEY, u.BTN_RIGHT, 0), (u.EV_SYN, 0, 0),
            (u.EV_REL, u.REL_WHEEL, 2), (u.EV_REL, u.REL_HWHEEL, -1), (u.EV_SYN, 0, 0)])

    def test_release_all_lets_go_of_every_button(self):
        p = u.Pointer(self.tmp.name)
        p.release_all()
        p.close()
        self.assertEqual(self.written(), [(u.EV_KEY, b, 0) for b in (u.BTN_LEFT, u.BTN_RIGHT, u.BTN_MIDDLE)] + [(u.EV_SYN, 0, 0)])

    def test_a_failing_setup_closes_the_descriptor(self):
        def boom(fd, req, arg=0):
            raise OSError("no uinput")
        with mock.patch.object(u.fcntl, "ioctl", boom), mock.patch.object(u.os, "close") as close:
            with self.assertRaises(OSError):
                u.Pointer(self.tmp.name)
        close.assert_called_once()

    def test_a_missing_device_raises(self):
        with self.assertRaises(OSError):
            u.Pointer("/nonexistent/uinput")


class KeyboardTests(FakeUinput):
    def test_tap_presses_and_releases_the_key(self):
        kb = u.Keyboard(self.tmp.name)
        kb.tap(u.KEY["Enter"])
        kb.close()
        self.assertEqual(self.written(), [(u.EV_KEY, 28, 1), (u.EV_SYN, 0, 0), (u.EV_KEY, 28, 0), (u.EV_SYN, 0, 0)])

    def test_shift_and_ctrl_wrap_the_key_and_come_off_in_reverse_order(self):
        kb = u.Keyboard(self.tmp.name)
        kb.tap(30, shift=True, ctrl=True)
        kb.close()
        keys = [(c, v) for t, c, v in self.written() if t == u.EV_KEY]
        self.assertEqual(keys, [(29, 1), (42, 1), (30, 1), (30, 0), (42, 0), (29, 0)])

    def test_the_keyboard_declares_every_key_it_can_type(self):
        kb = u.Keyboard(self.tmp.name)
        kb.close()
        declared = {a for r, a in self.ioctls if r == u.UI_SET_KEYBIT}
        for code in list(u.KEY.values()) + [u.char_to_key(c)[0] for c in "az09 ,./"]:
            self.assertIn(code, declared)
        self.assertIn(b"Piston Keyboard", [a for r, a in self.ioctls if r == u.UI_DEV_SETUP][0])

    def test_a_full_queue_drops_the_batch_quietly(self):
        kb = u.Keyboard(self.tmp.name)
        with mock.patch.object(u.os, "write", side_effect=BlockingIOError):
            kb.tap(30)
        kb.close()

    def test_release_all_covers_the_modifiers(self):
        kb = u.Keyboard(self.tmp.name)
        kb.release_all()
        kb.close()
        released = {c for t, c, v in self.written() if t == u.EV_KEY and v == 0}
        self.assertTrue({u.KEY["Ctrl"], u.KEY["Shift"], u.KEY["Enter"], 30} <= released)
