"""Download diagnostics with the real redaction helper, for a connected and for an offline device."""
import json

from custom_components.decky_home_assistant.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_hide_the_secret_and_the_address_but_keep_the_state(hass, config_entry, device, wait_until):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(lambda: config_entry.runtime_data.connected)
    await hass.async_block_till_done()

    report = await async_get_config_entry_diagnostics(hass, config_entry)
    text = json.dumps(report)
    for private in (config_entry.data["secret"], "127.0.0.1", "aa:bb:cc:dd:ee:ff", "10.0.0.255"):
        assert private not in text, private
    assert report["entry"]["data"]["secret"] == "**REDACTED**"
    assert report["coordinator"]["connected"] is True
    assert report["client"]["probe_mode"] == "connected"
    assert report["client"]["boot_id"] == device.server.boot_id
    assert report["groups"]["device"]["device_id"] == "dev123abc456"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_diagnostics_of_an_offline_device_return_what_was_last_known(hass, config_entry, device, wait_until):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    coordinator = config_entry.runtime_data
    await wait_until(lambda: coordinator.connected)
    await device.stop()                                           # the device goes away
    await wait_until(lambda: not coordinator.connected)

    report = await async_get_config_entry_diagnostics(hass, config_entry)
    json.dumps(report)
    assert report["coordinator"]["connected"] is False
    assert report["groups"]["device"]["name"] == "Box"             # last known
    assert report["client"]["probe_mode"] == "fast"
    assert report["coordinator"]["last_connected"] is not None
    assert await hass.config_entries.async_unload(config_entry.entry_id)
