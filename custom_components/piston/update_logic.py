"""Plugin self-update decisions: release addresses, SHA256SUMS and when to tell the device to update.

Pure functions (no Home Assistant imports) so every rule is tested without a running Home Assistant.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable

from .const import FIRST_LANE_VERSION, RELEASE_REPO

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SUMS_TIMEOUT = 15

# decide() outcomes
UP_TO_DATE, NO_LANE, PREDATES_LANE, LATCHED, AVAILABLE, SEND = (
    "up_to_date", "no_lane", "predates_lane", "latched", "available", "send")
CANNOT_DETERMINE = "cannot_determine"  # an update state, not a decide() outcome: the checksum could not be fetched


# Shown in the Repairs item of a failed update (the reasons the device reports, plus "version" from the integration)
REASON_TEXT = {
    "bad_request": "the device refused the request",
    "busy": "another update was already running",
    "download": "the device could not download the release",
    "hash": "the download did not match its checksum",
    "archive": "the downloaded file was not a valid plugin",
    "installer": "Decky Loader could not install it",
    "not_confirmed": "the install was not approved on the device",
    "version": "the plugin came back on a different version after the install",
}


def version_key(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def zip_name(version: str) -> str:
    return f"piston-{version}.zip"


def asset_url(version: str, name: str | None = None, repo: str = RELEASE_REPO) -> str:
    return f"https://github.com/{repo}/releases/download/v{version}/{name or zip_name(version)}"


def release_url(version: str, repo: str = RELEASE_REPO) -> str:
    return f"https://github.com/{repo}/releases/tag/v{version}"


def parse_sha256sums(text: str) -> dict[str, str]:
    """`sha256sum` output (`<hex64>  <name>`, `*` before the name for binary mode) -> {name: hash}; bad lines are skipped."""
    sums = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64}) [ *](.+)$", line.strip("\r"))
        if match:
            sums[match.group(2).strip()] = match.group(1).lower()
    return sums


async def zip_checksum(get_text: Callable[[str], Awaitable[str]], version: str, cache: dict) -> str | None:
    """The SHA-256 of the release zip for `version` from its SHA256SUMS asset, or None when it cannot be determined.

    `get_text(url)` fetches a URL as text (it raises on any failure). Successful results are cached per version."""
    if version in cache:
        return cache[version]
    try:
        sums = parse_sha256sums(await get_text(asset_url(version, "SHA256SUMS")))
    except Exception:  # noqa: BLE001 - offline, 404 or a timeout all mean "cannot determine"
        return None
    checksum = sums.get(zip_name(version))
    if checksum is not None:
        cache[version] = checksum
    return checksum


def decide(plugin_version: str | None, target: str, *, auto: bool, pressed: bool, latch: dict | None) -> str:
    """What to do about the device's plugin: one of the outcome constants above.

    `latch` is {"version", "reason"} of the last failed update. A press (Install) ignores both the auto setting and the latch."""
    if plugin_version is None or not VERSION_RE.match(plugin_version):
        return NO_LANE
    if plugin_version == target:
        return UP_TO_DATE
    if version_key(target) < version_key(FIRST_LANE_VERSION):
        return PREDATES_LANE
    if latch and latch.get("version") == target and not pressed:
        return LATCHED
    return SEND if (auto or pressed) else AVAILABLE
