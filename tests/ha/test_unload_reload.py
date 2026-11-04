"""config-entry-unloading: unload and reload leave nothing running (the fixtures also fail a test that leaks a timer or task)."""
import asyncio

from homeassistant.config_entries import ConfigEntryState


async def setup_and_connect(hass, config_entry, wait_until):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data
    await wait_until(lambda: coordinator.connected)
    await hass.async_block_till_done()
    return coordinator


async def test_unload_stops_the_connection_and_every_listener(hass, config_entry, device, wait_until):
    config_entry.add_to_hass(hass)
    coordinator = await setup_and_connect(hass, config_entry, wait_until)
    assert device.server.connected

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert not coordinator.client.connected
    assert coordinator.client._task.done()
    assert coordinator._listeners == []                  # entities, the store and the repair monitor all let go
    assert coordinator._off_handle is None
    for _ in range(50):                                  # the device sees the connection end
        if not device.server.connected:
            break
        await asyncio.sleep(0.02)
    assert not device.server.connected


async def test_reload_reconnects_with_a_new_coordinator(hass, config_entry, device, wait_until):
    config_entry.add_to_hass(hass)
    first = await setup_and_connect(hass, config_entry, wait_until)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    second = config_entry.runtime_data
    assert second is not first
    await wait_until(lambda: second.connected)
    assert hass.states.get("media_player.box").state == "idle"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_unload_writes_the_stored_state_through_the_real_store(hass, hass_storage, config_entry, device, wait_until):
    config_entry.add_to_hass(hass)
    await setup_and_connect(hass, config_entry, wait_until)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage[f"piston.{config_entry.entry_id}"]
    assert saved["version"] == 1
    assert saved["data"]["groups"]["device"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert "session" not in saved["data"]["groups"]["device"]        # volatile: not stored
    assert saved["data"]["last_connected"] is not None


async def test_after_a_restart_entities_come_from_the_stored_state_before_the_device_connects(hass, hass_storage, config_entry, device):
    hass_storage[f"piston.{config_entry.entry_id}"] = {
        "version": 1, "minor_version": 1, "key": f"piston.{config_entry.entry_id}",
        "data": {"last_connected": None, "groups": {
            "device": {"device_id": "dev123abc456", "name": "Box", "mac": "aa:bb:cc:dd:ee:ff"},
            "sensors": {"keys": ["cpu_load"]}, "apps": {"apps": [{"appid": "1", "name": "A"}]}}}}
    await device.stop()                                  # the device is off when Home Assistant starts
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data
    assert not coordinator.connected
    assert hass.states.get("media_player.box").state == "off"        # off, not unavailable: turn_on must still work
    assert hass.states.get("sensor.box_cpu_load").state == "unavailable"
    assert hass.states.get("media_player.box").attributes["source_list"] == ["Steam", "A"]
    assert await hass.config_entries.async_unload(config_entry.entry_id)
