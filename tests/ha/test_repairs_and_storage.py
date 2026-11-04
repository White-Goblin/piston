"""Repair issues in the real issue registry, and the stored state through the real Store with its real timer."""
from datetime import timedelta
from unittest import mock

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

import custom_components.piston as integration
from custom_components.piston.const import DOMAIN

from conftest import DEVICE_ID

UNREACHABLE = f"device_unreachable_{DEVICE_ID}"


def issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


# --- unreachable for a day ------------------------------------------------------------------------

async def test_unreachable_for_over_a_day_raises_a_warning_that_clears_on_the_next_connection(hass, loaded, device, wait_until):
    monitor = loaded.unreachable
    await device.stop()
    await wait_until(lambda: not loaded.connected)
    monitor._now = lambda: dt_util.utcnow() + timedelta(hours=25)
    await monitor._tick()
    found = issue(hass, UNREACHABLE)
    assert found is not None
    assert (found.is_fixable, found.severity, found.translation_key) == (False, ir.IssueSeverity.WARNING, "device_unreachable")
    assert found.translation_placeholders == {"name": "Box"}
    await device.start()                                              # the device is back on the same port
    loaded.client.wake()
    await wait_until(lambda: loaded.connected)
    await hass.async_block_till_done()
    assert issue(hass, UNREACHABLE) is None


async def test_the_hourly_check_runs_on_the_real_timer_and_keeps_the_stored_time_fresh(hass, hass_storage, loaded, config_entry):
    key = f"piston.{config_entry.entry_id}"
    hass_storage.pop(key, None)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1, seconds=5))
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["last_connected"] is not None     # connected: refreshed to now


async def test_removing_the_entry_removes_the_unreachable_issue(hass, config_entry, loaded, device, wait_until):
    await device.stop()
    await wait_until(lambda: not loaded.connected)
    loaded.unreachable._now = lambda: dt_util.utcnow() + timedelta(hours=25)
    loaded.unreachable.check()
    assert issue(hass, UNREACHABLE) is not None
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, UNREACHABLE) is None


async def test_a_device_offline_after_a_restart_for_longer_than_a_day_is_flagged_from_the_stored_time(hass, hass_storage, config_entry, device):
    hass_storage[f"piston.{config_entry.entry_id}"] = {
        "version": 1, "minor_version": 1, "key": f"piston.{config_entry.entry_id}",
        "data": {"groups": {}, "last_connected": (dt_util.utcnow() - timedelta(hours=30)).isoformat()}}
    await device.stop()
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, UNREACHABLE) is not None
    await hass.config_entries.async_unload(config_entry.entry_id)


# --- the version problem ------------------------------------------------------------------------------

async def test_a_version_mismatch_raises_an_error_that_names_the_side_to_update(hass, config_entry, device, wait_until):
    original = integration.DeckyClient
    with mock.patch.object(integration, "DeckyClient", lambda *a, **kw: original(*a, own=(9, 9), **kw)):
        config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(lambda: config_entry.runtime_data.problem == "version_skew_old")
    found = issue(hass, f"version_skew_old_{DEVICE_ID}")
    assert (found.severity, found.translation_key, found.is_fixable) == (ir.IssueSeverity.ERROR, "version_skew_old", False)
    assert hass.states.get("media_player.box").state == "unavailable"       # a problem, so not "off"
    await hass.config_entries.async_unload(config_entry.entry_id)


# --- stored state through the real Store --------------------------------------------------------------

async def write_now(hass, coordinator):
    """Make the Store's pending delayed write happen (as its timer does). Advancing the whole loop's clock instead would also
    fire the fake device's idle timeouts and drop the connection."""
    await coordinator.store._store._async_callback_delayed_write()
    await hass.async_block_till_done()


def write_pending(coordinator) -> bool:
    return coordinator.store._store._delay_handle is not None


async def test_a_change_in_the_stable_state_is_saved_through_a_delayed_write(hass, hass_storage, config_entry, loaded, device, wait_until):
    key = f"piston.{config_entry.entry_id}"
    await write_now(hass, loaded)                                        # what the connection itself changed
    hass_storage.pop(key, None)
    device.push("apps", {"apps": [{"appid": "1", "name": "A", "kind": "steam"}], "running": False})
    await wait_until(lambda: hass.states.get("media_player.box").attributes.get("source_list") == ["Steam", "A"])
    assert write_pending(loaded) and key not in hass_storage             # a delayed save is requested, nothing written yet
    await write_now(hass, loaded)
    assert hass_storage[key]["data"]["groups"]["apps"] == {"apps": [{"appid": "1", "name": "A", "kind": "steam"}]}


async def test_readings_that_change_do_not_request_writes(hass, hass_storage, config_entry, loaded, device, wait_until):
    key = f"piston.{config_entry.entry_id}"
    device.push("sensors", {"cpu_load": 1})
    await wait_until(lambda: getattr(hass.states.get("sensor.box_cpu_load"), "state", None) == "1")
    await write_now(hass, loaded)                                        # the new sensor key is stable data: saved once
    assert hass_storage[key]["data"]["groups"]["sensors"] == {"keys": ["cpu_load"]}
    hass_storage.pop(key)
    for load in range(2, 8):                                             # from now on only the readings change
        device.push("sensors", {"cpu_load": load})
    await wait_until(lambda: getattr(hass.states.get("sensor.box_cpu_load"), "state", None) == "7")
    assert not write_pending(loaded)                                     # no save was requested
    await write_now(hass, loaded)
    assert key not in hass_storage


async def test_stopping_home_assistant_saves_the_state(hass, hass_storage, config_entry, loaded):
    key = f"piston.{config_entry.entry_id}"
    hass_storage.pop(key, None)
    hass.bus.async_fire("homeassistant_stop")
    await hass.async_block_till_done()
    assert key in hass_storage


async def test_state_from_a_newer_version_or_a_bad_shape_is_ignored_with_one_warning(hass, hass_storage, config_entry, device, caplog):
    key = f"piston.{config_entry.entry_id}"
    hass_storage[key] = {"version": 99, "minor_version": 1, "key": key, "data": {"groups": {"apps": {"apps": [{"appid": "9", "name": "X"}]}}}}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert caplog.text.count("Ignoring the stored device state") == 1
    assert config_entry.runtime_data.restored <= {"device", "apps", "audio", "controllers", "sensors"}
    assert "X" not in hass.states.get("media_player.box").attributes.get("source_list", [])
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_an_older_stored_version_and_a_bad_timestamp_are_handled_quietly(hass, hass_storage, config_entry, device, caplog):
    key = f"piston.{config_entry.entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"groups": {}, "last_connected": "yesterday"}}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert "Ignoring the stored device state" not in caplog.text
    await hass.config_entries.async_unload(config_entry.entry_id)
