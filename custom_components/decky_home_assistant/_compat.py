"""Schema library: Home Assistant moved from voluptuous to probatio (same API for what we use)."""
try:
    import probatio as vol  # noqa: F401
except ImportError:  # older Home Assistant
    import voluptuous as vol  # noqa: F401
