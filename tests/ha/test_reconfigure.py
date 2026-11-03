"""The reconfigure flow through Home Assistant's real flow manager: the address changes, the pairing stays, the entry reloads."""
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType


async def test_reconfigure_moves_the_entry_to_a_new_address_and_reloads(hass, config_entry, device, wait_until):
    config_entry.add_to_hass(hass)
    # the entry was saved with a stale address; the device is really on loopback at device.port
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, "host": "192.168.1.250", "port": 9})
    secret = config_entry.data["secret"]

    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "port": device.port})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    assert (config_entry.data["host"], config_entry.data["port"]) == ("127.0.0.1", device.port)
    assert config_entry.data["secret"] == secret
    assert config_entry.state is ConfigEntryState.LOADED
    await wait_until(lambda: config_entry.runtime_data.connected)
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_reconfigure_with_a_wrong_address_keeps_the_form_and_the_entry(hass, config_entry, device):
    config_entry.add_to_hass(hass)
    before = dict(config_entry.data)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "port": 1})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "cannot_connect"}
    assert dict(config_entry.data) == before
    hass.config_entries.flow.async_abort(result["flow_id"])               # the user gives up


async def test_reconfigure_pointing_at_another_device_aborts(hass, config_entry, device):
    config_entry.add_to_hass(hass)
    device.server._device_id = lambda: "someone-else"                # a different device now answers at that address
    before = dict(config_entry.data)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "port": device.port})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_device"
    assert dict(config_entry.data) == before
