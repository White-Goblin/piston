"""The integration sets up, connects to a (fake) device, creates its entities, and unloads without leaving anything running."""
from homeassistant.config_entries import ConfigEntryState


async def test_setup_connect_and_unload(hass, config_entry, device, wait_until):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED

    coordinator = config_entry.runtime_data
    await wait_until(lambda: coordinator.connected)
    await hass.async_block_till_done()

    player = hass.states.get("media_player.box")
    assert player is not None
    assert player.state == "idle"                  # connected and awake, nothing running
    assert hass.states.get("binary_sensor.box_connectivity").state == "on"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
