"""The Sendspin switch and status sensor in a real Home Assistant, against the real protocol server on loopback."""
import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

SWITCH = "switch.box_sendspin_player"
STATUS = "sensor.box_sendspin_status"
GROUP = {"enabled": True, "status": "waiting", "name": "Box", "port": 8928}


async def call(hass, service, entity_id=SWITCH):
    await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)


async def test_a_device_without_a_sendspin_player_has_no_entities(hass, loaded, state):
    assert state(SWITCH) is None
    assert state(STATUS) is None


async def test_the_entities_appear_when_the_device_reports_the_group(hass, loaded, device, state, wait_until):
    device.push("sendspin", GROUP)
    await wait_until(lambda: state(SWITCH) is not None and state(STATUS) is not None)
    await hass.async_block_till_done()
    assert (state(SWITCH), state(STATUS)) == ("on", "waiting")
    attributes = hass.states.get(STATUS).attributes
    assert attributes["device_class"] == "enum"
    assert attributes["options"] == ["disabled", "waiting", "connected", "streaming", "error"]
    assert (attributes["port"], attributes["player_name"], attributes["server_name"]) == (8928, "Box", None)
    assert hass.states.get(SWITCH).attributes["friendly_name"] == "Box Sendspin player"
    assert attributes["friendly_name"] == "Box Sendspin status"


async def test_the_registry_has_the_categories_the_unique_ids_and_the_device(hass, loaded, device, state, wait_until):
    device.push("sendspin", GROUP)
    await wait_until(lambda: state(SWITCH) is not None and state(STATUS) is not None)
    registry = er.async_get(hass)
    switch, sensor = registry.async_get(SWITCH), registry.async_get(STATUS)
    assert (switch.entity_category, sensor.entity_category) == ("config", "diagnostic")
    assert (switch.unique_id, sensor.unique_id) == ("dev123abc456_sendspin_player", "dev123abc456_sendspin_status")
    assert switch.device_id and switch.device_id == sensor.device_id == er.async_get(hass).async_get("media_player.box").device_id
    assert dr.async_get(hass).async_get(switch.device_id).name == "Box"
    assert (switch.translation_key, sensor.translation_key) == ("sendspin_player", "sendspin_status")


async def test_the_status_follows_the_device(hass, loaded, device, state, wait_until):
    device.push("sendspin", GROUP)
    await wait_until(lambda: state(STATUS) == "waiting")
    device.push("sendspin", {**GROUP, "status": "connected", "server_name": "Music Assistant"})
    await wait_until(lambda: state(STATUS) == "connected")
    assert hass.states.get(STATUS).attributes["server_name"] == "Music Assistant"
    device.push("sendspin", {**GROUP, "status": "streaming", "server_name": "Music Assistant"})
    await wait_until(lambda: state(STATUS) == "streaming")
    device.push("sendspin", {**GROUP, "status": "error", "error": "no free port from 8928 to 8937"})
    await wait_until(lambda: state(STATUS) == "error")
    assert hass.states.get(STATUS).attributes["error"] == "no free port from 8928 to 8937"


async def test_the_switch_sends_the_command_and_follows_what_the_device_reports(hass, loaded, device, state, wait_until):
    device.push("sendspin", GROUP)
    await wait_until(lambda: state(SWITCH) == "on")
    await call(hass, "turn_off")
    assert device.sent("sendspin_enable") == [{"enabled": False}]
    assert state(SWITCH) == "on"                                         # the device has not reported yet: no guessing
    device.push("sendspin", {"enabled": False, "status": "disabled", "name": "Box"})
    await wait_until(lambda: state(SWITCH) == "off")
    assert state(STATUS) == "disabled"
    await call(hass, "turn_on")
    assert device.sent("sendspin_enable")[-1] == {"enabled": True}


async def test_a_command_the_device_refuses_is_an_error_for_the_caller(hass, loaded, device, state, wait_until):
    device.push("sendspin", GROUP)
    await wait_until(lambda: state(SWITCH) == "on")
    device.fail["sendspin_enable"] = "internal"
    with pytest.raises(HomeAssistantError, match="failed on purpose"):
        await call(hass, "turn_off")
    assert state(SWITCH) == "on"


async def test_both_are_unavailable_while_the_device_sleeps_and_return_with_the_group(hass, loaded, device, state, wait_until):
    device.push("sendspin", GROUP)
    await wait_until(lambda: state(SWITCH) == "on")
    device.event("going_to_sleep", {"reason": "suspend"})
    await wait_until(lambda: state(SWITCH) == "unavailable" and state(STATUS) == "unavailable")
    device.event("resumed")
    device.push("sendspin", {**GROUP, "status": "connected"})
    await wait_until(lambda: state(STATUS) == "connected" and state(SWITCH) == "on")


async def test_after_a_reload_the_entities_come_back_from_the_devices_snapshot(hass, config_entry, loaded, device, state, wait_until):
    device.push("sendspin", {**GROUP, "status": "connected"})
    await wait_until(lambda: state(STATUS) == "connected")
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data
    await wait_until(lambda: coordinator.connected and state(STATUS) == "connected")
    registry = er.async_get(hass)
    assert len([e for e in registry.entities.values() if e.platform == "piston" and "sendspin" in e.unique_id]) == 2   # no duplicates


async def test_the_group_is_live_state_and_not_stored(hass, config_entry, loaded, device, state, wait_until):
    from custom_components.piston.store import stable_subset
    device.push("sendspin", GROUP)
    await wait_until(lambda: state(SWITCH) == "on")
    assert "sendspin" in loaded.groups
    assert "sendspin" not in stable_subset(loaded.groups)                # what the Store would write has no player status
    assert "sendspin" not in loaded.restored


async def test_diagnostics_include_the_group(hass, config_entry, loaded, device, wait_until):
    from custom_components.piston.diagnostics import async_get_config_entry_diagnostics
    device.push("sendspin", {**GROUP, "status": "connected", "server_name": "Music Assistant"})
    await wait_until(lambda: loaded.sendspin is not None and loaded.sendspin["status"] == "connected")
    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert report["groups"]["sendspin"]["server_name"] == "Music Assistant"
