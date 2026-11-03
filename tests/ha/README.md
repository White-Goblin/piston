# Tests against a real Home Assistant

The stdlib suite in `tests/` runs everything with stubs in place of Home Assistant, which is fast and has no dependencies
but cannot prove Home Assistant-specific code (config flows, entity registration, availability, the Store, repair issues).
This folder runs the integration inside a real Home Assistant using
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component).

It is a development tool only: the plugin and the normal test run need none of it, and
`python3 -m unittest discover -s tests -t .` does not touch this folder.

```
uv venv .venv-ha --python 3.14
uv pip install --python .venv-ha/bin/python -r tests/ha/requirements.txt
.venv-ha/bin/python -m pytest tests/ha -q
# with coverage of the integration (configuration in tests/ha/.coveragerc):
.venv-ha/bin/python -m pytest tests/ha -q --cov --cov-config=tests/ha/.coveragerc
```

## What the coverage number covers

`.coveragerc` measures the `custom_components/decky_home_assistant` package and leaves out `proto/`, which is a verbatim copy of
`decky_ha/proto` (kept identical by `scripts/sync_proto.py` and a test); much of it only runs on the device. Counting the copy as
well still gives 95%. `test_client_protocol.py` runs the client's own unit tests (they use no Home Assistant classes) under this
suite so the pairing, authentication and reconnect paths that need a misbehaving or absent device are included; without them the
configured number is 97%.

`requirements.txt` pins the release that matches the Home Assistant version the integration was verified against
(2026.10.4). The device is played by the real `decky_ha` protocol server on the loopback interface, so the tests
exercise the real protocol end to end.

The fixtures also fail a test that leaves a timer or task running after the entry is unloaded, which is how
`config-entry-unloading` (quality scale, Silver) is checked.
