"""Schema library: Home Assistant moved from voluptuous to probatio (same API for what we use)."""
try:
    import probatio as vol  # noqa: F401
except ImportError:  # pragma: no cover - older Home Assistant; the tested release ships probatio
    import voluptuous as vol  # noqa: F401
