import json
import unittest

from custom_components.piston import input_status as s
from custom_components.piston.diagnostics import async_get_config_entry_diagnostics
from .entityutil import connect, make_coordinator

ON = {"input": {"enabled": True}, "session": "game"}


def status(**kw):
    args = dict(power="on", connected=True, sleeping=False, skewed=None, problem=None, device_group=ON)
    args.update(kw)
    return s.input_status(**args)


class StatusFunctionTests(unittest.TestCase):
    """Each row of the card's state table (features/remote-control-card.md)."""

    def test_on_in_game_mode_is_ready(self):
        self.assertEqual(status(), s.READY)

    def test_on_in_desktop_mode_is_desktop(self):
        self.assertEqual(status(device_group={**ON, "session": "desktop"}), s.DESKTOP)

    def test_off_or_asleep_is_device_off(self):
        self.assertEqual(status(power="off", connected=False), s.DEVICE_OFF)
        self.assertEqual(status(power="standby", connected=False, sleeping=True), s.DEVICE_OFF)
        self.assertEqual(status(sleeping=True), s.DEVICE_OFF)           # the connection may outlive a suspend
        self.assertEqual(status(power="off"), s.DEVICE_OFF)             # announced shutdown, socket still open

    def test_a_silent_drop_is_unreachable_until_the_off_timer_fires(self):
        self.assertEqual(status(connected=False), s.UNREACHABLE)

    def test_a_pairing_problem_is_unreachable(self):
        self.assertEqual(status(connected=False, problem="auth", power="off"), s.UNREACHABLE)

    def test_a_plugin_without_the_field_is_no_capability(self):
        self.assertEqual(status(device_group={"session": "game"}), s.NO_CAPABILITY)
        self.assertEqual(status(device_group=None), s.NO_CAPABILITY)
        self.assertEqual(status(device_group={"input": "yes"}), s.NO_CAPABILITY)

    def test_switched_off_on_the_device_is_disabled(self):
        self.assertEqual(status(device_group={"input": {"enabled": False}, "session": "desktop"}), s.DISABLED)

    def test_an_error_from_the_device_is_error_only_while_input_is_on(self):
        self.assertEqual(status(device_group={**ON, "input": {"enabled": True, "error": "uinput is not writable"}}), s.ERROR)
        self.assertEqual(status(device_group={**ON, "input": {"enabled": False, "error": "x"}}), s.DISABLED)
        self.assertEqual(status(device_group={**ON, "input": {"enabled": True, "error": ""}}), s.READY)

    def test_skew_wins_over_everything(self):
        self.assertEqual(status(skewed="old", connected=False, power="off"), s.SKEW)
        self.assertEqual(status(skewed="new"), s.SKEW)

    def test_every_status_is_listed(self):
        seen = {status(), status(device_group={**ON, "session": "desktop"}), status(power="off", connected=False),
                status(connected=False), status(device_group={}), status(device_group={"input": {"enabled": False}}),
                status(device_group={"input": {"enabled": True, "error": "x"}}), status(skewed="old")}
        self.assertEqual(seen, set(s.STATUSES))

    def test_capability(self):
        self.assertEqual(s.capability(None), s.UNSUPPORTED)
        self.assertEqual(s.capability({}), s.UNSUPPORTED)
        self.assertEqual(s.capability({"input": {"enabled": True}}), s.CAP_ENABLED)
        self.assertEqual(s.capability({"input": {"enabled": False}}), s.CAP_DISABLED)


class CoordinatorInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_listeners_fire_on_a_change_only(self):
        coord = make_coordinator()
        self.addCleanup(coord.shutdown)
        heard = []
        coord.async_add_input_listener(heard.append)
        coord.on_connection(True)
        coord.on_state("device", {"name": "Box", "session": "game"})          # connected, no capability
        coord.on_state("sensors", {"cpu_load": 1})                           # no change
        coord.on_state("device", {"name": "Box", "session": "game", "input": {"enabled": True}})
        coord.on_state("device", {"name": "Box", "session": "game", "input": {"enabled": True}, "mac": "x"})  # no change
        coord.on_state("device", {"name": "Box", "session": "desktop", "input": {"enabled": True}})
        coord.on_event("going_to_sleep", {"reason": "suspend"})
        self.assertEqual(heard, [s.NO_CAPABILITY, s.READY, s.DESKTOP, s.DEVICE_OFF])
        self.assertEqual(coord.input_status, s.DEVICE_OFF)

    async def test_a_new_listener_does_not_hear_the_status_it_already_knows(self):
        coord = make_coordinator()
        self.addCleanup(coord.shutdown)
        connect(coord, device={"session": "game", "input": {"enabled": True}})
        heard = []
        coord.async_add_input_listener(heard.append)
        coord._notify()
        self.assertEqual(heard, [])
        self.assertEqual(coord.input_capability, s.CAP_ENABLED)

    async def test_removed_listeners_are_silent(self):
        coord = make_coordinator()
        self.addCleanup(coord.shutdown)
        heard = []
        remove = coord.async_add_input_listener(heard.append)
        remove()
        coord.on_connection(True)
        self.assertEqual(heard, [])

    async def test_skew_is_reported(self):
        coord = make_coordinator()
        self.addCleanup(coord.shutdown)
        connect(coord, device={"session": "game", "input": {"enabled": True}})
        coord.skewed = "old"
        self.assertEqual(coord.input_status, s.SKEW)


class DiagnosticsInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_status_and_counters_are_reported_and_no_content(self):
        coord = make_coordinator()
        self.addCleanup(coord.shutdown)
        coord.entry.runtime_data = coord
        connect(coord, device={"device_id": "dev123abc456", "name": "Box", "session": "game", "input": {"enabled": True}})
        coord.client.input_frames, coord.client.input_dropped = 12, 3
        report = await async_get_config_entry_diagnostics(None, coord.entry)
        self.assertEqual(report["input"], {"capability": "enabled", "status": "ready", "frames_written": 12, "events_dropped": 3})
        self.assertNotIn("text", json.dumps(report["input"]))
