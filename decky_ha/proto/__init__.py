"""Wire protocol shared by the plugin and the integration (stdlib only).

Source of truth is `decky_ha/proto/`; `scripts/sync_proto.py` copies it into
`custom_components/decky_home_assistant/proto/`. Keep it free of plugin and Home Assistant dependencies.
"""

# Version 2: state frames carry `boot_id` and a per-group `seq`; `resync` command; `shutting_down` event.
# Version 3: `media` state group and the `get_artwork` command (chunked image bytes).
# No older version support: a v1 or v2 peer gets the version-skew result.
PROTOCOL_VERSION = 3
MIN_PROTOCOL_VERSION = 3

# The update lane (self-update): the device `hello`, the challenge/proof exchange and the close codes are frozen from the
# release that introduced it, so a device and an integration of any protocol version can still reach each other to
# update the plugin (see docs/protocol.md). `LANE_VERSION` is advertised in the device hello.
LANE_VERSION = 1
