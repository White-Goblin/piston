"""Test package: installs a fake `decky` module before any `decky_ha` import."""
import logging
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))

_settings_dir = tempfile.mkdtemp(prefix="decky-ha-settings-")

fake = types.ModuleType("decky")
fake.logger = logging.getLogger("decky-test")
fake.logger.addHandler(logging.NullHandler())  # keep test output quiet
fake.DECKY_PLUGIN_SETTINGS_DIR = _settings_dir
fake.DECKY_PLUGIN_DIR = ROOT
fake.emitted = []  # (event, args) recorded by emit


async def _emit(event, *args):
    fake.emitted.append((event, args))


fake.emit = _emit
sys.modules["decky"] = fake

from . import ha_stubs  # noqa: E402

ha_stubs.install()


def use_temp_settings_dir() -> str:
    """Point the fake decky at a fresh empty settings dir and return it."""
    fake.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp(prefix="decky-ha-settings-")
    return fake.DECKY_PLUGIN_SETTINGS_DIR
