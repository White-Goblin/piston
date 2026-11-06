"""The remote card ships with the integration: one JavaScript file, served from a static path and added to every dashboard
page with `frontend.add_extra_js_url`, so there is no Lovelace resource to add by hand (HACS accepts it: everything stays inside
custom_components/piston/). The address carries the integration version and a fingerprint of the file, so it changes whenever the card does, which is what makes
the Companion app load the new file instead of its cached copy."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/piston_static"
CARD_FILE = "piston-remote-card.js"
WWW_DIR = Path(__file__).parent / "www"
_REGISTERED = "piston_card_registered"


def _content_tag() -> str:
    """A short fingerprint of the card file: the address changes whenever the file does, even between two releases."""
    try:
        return hashlib.sha256((WWW_DIR / CARD_FILE).read_bytes()).hexdigest()[:8]
    except OSError:
        return ""


def card_url(version: str | None, tag: str = "") -> str:
    suffix = "-".join(part for part in (version, tag) if part)
    return f"{URL_BASE}/{CARD_FILE}" + (f"?v={suffix}" if suffix else "")


async def async_register_card(hass: HomeAssistant, version: str | None) -> None:
    """Serve and register the card once per Home Assistant run. A failure only costs the card: it is logged, not raised."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    try:
        await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, str(WWW_DIR), False)])
        tag = await hass.async_add_executor_job(_content_tag)
        add_extra_js_url(hass, card_url(version, tag))
    except Exception:  # noqa: BLE001 - the integration works without its card
        _LOGGER.warning("Could not register the Piston remote card; the rest of the integration is not affected", exc_info=True)
