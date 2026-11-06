"""The remote card is served and added to the frontend by the integration, once, with a versioned URL."""
from unittest import mock

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

from custom_components.piston import card
from custom_components.piston.const import DOMAIN


async def test_the_card_is_added_to_the_frontend_and_served(hass, loaded, hass_client):
    version = hass.data["integrations"][DOMAIN].version
    (url,) = [u for u in hass.data[DATA_EXTRA_MODULE_URL].urls if "piston-remote-card" in u]
    assert url.startswith(f"/piston_static/piston-remote-card.js?v={version}-")
    client = await hass_client()
    response = await client.get("/piston_static/piston-remote-card.js")
    assert response.status == 200
    assert "piston-remote-card" in await response.text()
    assert (await client.get("/piston_static/nothing.js")).status == 404


async def test_a_second_entry_registers_nothing_again(hass, loaded):
    urls = set(hass.data[DATA_EXTRA_MODULE_URL].urls)
    await card.async_register_card(hass, "9.9.9")
    assert hass.data[DATA_EXTRA_MODULE_URL].urls == urls


async def test_a_failing_registration_does_not_stop_the_integration(hass, config_entry, device, wait_until, caplog):
    config_entry.add_to_hass(hass)
    with mock.patch.object(card, "add_extra_js_url", side_effect=RuntimeError("frontend gone")):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await wait_until(lambda: config_entry.runtime_data.connected)
    assert "Could not register the Piston remote card" in caplog.text
    assert await hass.config_entries.async_unload(config_entry.entry_id)
