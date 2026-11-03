"""Wire protocol shared by the plugin and the integration (stdlib only).

Source of truth is `decky_ha/proto/`; `scripts/sync_proto.py` copies it into
`custom_components/decky_home_assistant/proto/`. Keep it free of plugin and Home Assistant dependencies.
"""

# Version 2: state frames carry `boot_id` and a per-group `seq`; `resync` command; `shutting_down` event.
# No version 1 support: a v1 peer gets the version-skew result.
PROTOCOL_VERSION = 2
MIN_PROTOCOL_VERSION = 2
