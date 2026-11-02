"""Wire protocol shared by the plugin and the integration (stdlib only).

Source of truth is `decky_ha/proto/`; `scripts/sync_proto.py` copies it into
`custom_components/decky_home_assistant/proto/`. Keep it free of plugin and Home Assistant dependencies.
"""

PROTOCOL_VERSION = 1
MIN_PROTOCOL_VERSION = 1
