# Piston: rules for agents and maintainers

Piston is a Home Assistant integration (HACS, `custom_components/piston/`) plus a mostly headless Decky Loader plugin (`main.py`, `piston_device/`, `src/`) that together control a SteamOS device. Use hardware-agnostic wording ("device") and never hard-code screen sizes. BSD-3-Clause; the public docs are `README.md` and `docs/`. How the pieces fit: [docs/architecture.md](docs/architecture.md). Where things live: [docs/modules.md](docs/modules.md). Which tests to use: [docs/testing.md](docs/testing.md). What was rejected or accepted, so it is not reopened: [docs/decisions.md](docs/decisions.md). Read decisions.md before proposing new work. Build, test and device commands: [docs/development.md](docs/development.md). Release steps: [docs/releasing.md](docs/releasing.md).

## Rules

Each rule names the test or script that guards it; `none (convention)` means nothing enforces it yet, so it is on you.

| Rule | Why | Guard |
|------|-----|-------|
| Protocol code lives in `piston_device/proto/` and is copied into the integration with `python3 scripts/sync_proto.py`; never edit the copy. | Both sides must run byte-identical framing and authentication code. | `tests/test_proto_sync.py::ProtoSyncTests` |
| The handshake, close codes and update frames are frozen. | A plugin that cannot take the update lane has to be installed by hand. | `tests/test_frozen_handshake.py::FrozenHandshake` |
| Protocol changes are additive: never remove or rename a command, state group, event, error code or hello key. | A paired peer on another version must not break. | `tests/test_protocol_additive.py::AdditiveProtocolTests` |
| Device code imports only the standard library, Decky and itself; system libraries go through `ctypes`. | Decky has no runtime pip. | `tests/test_device_imports.py::DeviceImportTests` |
| Every long-lived device component has a synchronous `abort()` that `Plugin._unload_now` calls. | Decky starves the event loop after the unload request and kills the plugin after five seconds. | `tests/test_abort_everywhere.py::AbortEverywhereTests` |
| Never use `tabsHook.add`; no hard-coded screen sizes or device model names in code or user-facing text. | `tabsHook.add` corrupts the Steam menu, and the plugin runs on several devices. | `tests/test_ui_rules.py::UiRuleTests` |
| Keep `README.md` tables and `docs/` in step with the behavior. | Users read them. | `tests/test_docs_and_packaging.py::DocsMatchTheCode` |
| Version, changelog and tag agree at release; the engineer must say so before you tag, change repository visibility, rewrite public history, revoke tokens or push private history. | HACS reads the tag, and these actions are hard to undo. | `scripts/check_release.py`; `tests/test_release_policy.py::ReleaseConsistencyTests` |
| No emails, MAC addresses, private IPs, personal names or retired project names in the tree; private notes go in the git-ignored `AGENTS.local.md`. | The repository is public. | `scripts/check_public.py`; `tests/test_public_clean.py::PublicTreeTests` |
| Commits end with the `Co-Authored-By` trailer given in the session; the engineer verifies, then says "commit and push". | Attribution and review. | none (convention) |
| Only the Sendspin session's audio task touches the audio output, with a time limit on every call (the guard checks who calls the sink; the time limits are on you). | A hung output once froze the whole session. | `tests/test_convention_guards.py::AudioOwnerTests` |
| Never run `pgrep -f` or `pkill -f` with a pattern that appears in your own command line (use `[x]yz` brackets or a PID; the guard scans tracked files only). | It kills your own shell. | `tests/test_convention_guards.py::NoSelfKillTests` |
| Tests that run `Plugin._main()` use `tests.plugin_environment_patches()`. | Otherwise they start `gdbus` on the host. | `tests/test_convention_guards.py::PluginEnvironmentTests` |
| Every source file has a line in `docs/modules.md`. | Agents find the right file without reading the tree. | `tests/test_convention_guards.py::ModuleMapTests` |
| Every integration platform sets `PARALLEL_UPDATES`, and failed device commands map through `command_failure`. | Home Assistant quality scale. | `tests/test_quality_rules.py::ParallelUpdatesTests` |

## Current state
- Version 0.9.0 (seeking from the remote card), released; `main` is the release branch.
- Work happens on a branch per effort; merge only after the engineer verifies.
- Local notes, the dev device and the planning tree are in `AGENTS.local.md` (git-ignored). If it is missing (a fresh clone, CI), everything needed to build, test and release is in `docs/`; ask the engineer for device access and never invent addresses.

## Commands
The full list is in [docs/development.md](docs/development.md); the usual gate is `python3 -m unittest discover -s tests -t .` plus `python3 scripts/check_public.py`.
