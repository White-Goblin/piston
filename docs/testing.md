# Testing guide

Which suite to use, the helpers each one gives you, and what is deliberately not tested. Commands are in [development.md](development.md); the rules the tests guard are in `AGENTS.md`.

## Which suite for which change

| Change | Suite | Notes |
|--------|-------|-------|
| Plugin backend (`piston_device/`, `main.py`) | `tests/` (unittest) | Fast, standard library only. |
| Shared protocol (`piston_device/proto/`) | `tests/` | Run `python3 scripts/sync_proto.py` too; `test_proto_sync.py` fails when the copy differs. |
| Integration logic that does not need Home Assistant itself | `tests/` | Runs against `tests/ha_stubs`. |
| Config flow, entity registration, availability, Store, repairs, anything Home Assistant specific | `tests/ha` (pytest) | A real Home Assistant; coverage floor 95%. Setup in `tests/ha/README.md`. |
| The remote card | `tests/js` (`node --test`) | Pure logic plus a fake DOM; `harness.html` for a look in a browser. |
| The Steam frontend (`src/`) | none | Only `tsc`, the build and the UI-rule scan (`test_ui_rules.py`). Behavior is checked on a device. Keep logic in small pure functions where you can, and say in the change what you checked by hand. |
| Sendspin player | `tests/` | A scripted server over loopback and a fake sink; no PipeWire or network. |

## Helpers

- `tests/__init__.py`: installs a fake `decky` module before any device import (so import `piston_device` only through the `tests` package), the stand-in Home Assistant, `use_temp_settings_dir()` for a fresh settings directory, `StubLifecycle`, and `plugin_environment_patches()`.
- **`plugin_environment_patches()` is required** in any test that awaits `Plugin._main()`: without it the test starts `gdbus` on the host and depends on its desktop. `test_convention_guards.py` fails a test file that forgets.
- `tests/ha_stubs/`: small stand-ins for the Home Assistant modules the integration imports. They are installed only when the real `homeassistant` package is not importable, so the same test file can run against either. Add a name only when integration code needs it, and prove Home Assistant behavior in `tests/ha`, not against a stub.
- `tests/wsclient.py`: a tiny masked-frame websocket client against `piston_device.server`.
- `tests/entityutil.py`, `tests/flowutil.py`: a coordinator with a recording fake client, and config-flow helpers.
- `tests/stdlib_transport.py`: a standard-library `Transport` for client tests.
- `tests/sendspin_peer.py`: the scripted Sendspin server; `tests/sendspin_*_fixtures.py` and `sendspin_noise_vectors.py` hold real captured chunks and reference transcripts.
- `tests/protocol_snapshot.json`: the frozen protocol surface checked by `test_protocol_additive.py`. Edit it only for a deliberate breaking change (see [architecture.md](architecture.md)).

## Conventions

- A test file is `tests/test_<module>.py` and names its subject in its docstring. Rule guards also state, in the docstring, what to do when they fail.
- New async tests use `unittest.IsolatedAsyncioTestCase`. Never sleep for real time; the components take injectable clocks and sleeps.
- Never touch the real `~/homebrew`, the network, `gdbus`, `pw-cat` or `/dev/uinput`: use the fakes.
- A new rule gets a guard in the same change, or an honest `none (convention)` row.

## Accepted gaps

See [decisions.md](decisions.md) for checks that were deliberately left untested and should not be raised again.
