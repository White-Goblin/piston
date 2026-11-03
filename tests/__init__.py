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


class StubLifecycle:
    """Stands in for decky_ha.lifecycle.Lifecycle in tests that run Plugin._main(), so they never start gdbus or take a
    real sleep inhibitor on the machine running the tests."""
    instances = []

    def __init__(self, **callbacks):
        self.callbacks, self.active, self.protecting, self.started, self.stopped = callbacks, False, False, False, False
        StubLifecycle.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def plugin_environment_patches():
    """Patches that make Plugin._main() independent of the machine the tests run on: no real lifecycle monitor, and a
    fixed session (the real detector looks at this machine's processes, and a desktop here would refuse game-mode commands)."""
    from unittest import mock
    return [mock.patch("decky_ha.state.Lifecycle", StubLifecycle),
            mock.patch("decky_ha.state.detect_session", lambda comms=None: "game")]
