# Architecture and invariants

How the pieces fit, and the rules that must keep holding. Each rule has a reason; the table in `AGENTS.md` lists the rules with the test or script that guards each one. Behavior is described in [protocol.md](protocol.md) and [reference.md](reference.md); build and test commands are in [development.md](development.md).

## Components and data flow

```
 Home Assistant                                  SteamOS device (Decky Loader)
┌────────────────────────────┐   mDNS advert   ┌──────────────────────────────────┐
│ custom_components/piston   │◀────────────────│ plugin backend  (piston_device/) │
│  config flow, coordinator, │   websocket     │  websocket server, mDNS, handlers│
│  entities, update, card    │◀───────────────▶│  state groups, Sendspin player   │
└────────────────────────────┘   (LAN, 47830)  └───────────┬──────────────────────┘
                                                    loopback │ events, long-poll, HTTP
                                                ┌───────────▼──────────────────────┐
                                                │ Steam frontend (src/)            │
                                                │  panel, toasts, PiP and card UI  │
                                                └──────────────────────────────────┘
```

- **Integration** (`custom_components/piston/`): finds the device by mDNS, pairs with a 6-digit code (SRP-6a), stores a per-device secret and authenticates every connection with a mutual HMAC proof. It owns almost all configuration. No Home Assistant credential is ever stored on the device.
- **Plugin backend** (`main.py`, `piston_device/`): a websocket server, a standard-library mDNS responder, command handlers and the state groups it pushes. It runs as a system service, so it keeps running in desktop mode; commands that need the Steam interface answer `unsupported` there.
- **Steam frontend** (`src/`): runs inside the Steam interface and does what only it can: toasts, the picture-in-picture view, the cursor overlay, the media tab, plugin installation. It talks to the backend through Decky callables, events and a nonce-protected loopback HTTP server, never to Home Assistant directly.
- **Remote card** (`custom_components/piston/www/piston-remote-card.js`): one hand-written file with no build step, served by the integration.

## Invariants

### The protocol code is shared and copied
`piston_device/proto/` is the only copy that is edited. `python3 scripts/sync_proto.py` copies it byte for byte into the integration, and a test fails when they differ. **Why:** both sides must run identical framing, authentication and validation code, and the integration cannot import from the plugin.

### The handshake is frozen; the protocol only grows
The `hello` keys, the `challenge`/`proof` exchange, the close codes and the `update`/`update_status` frames never change. Other changes are additive: new state groups, commands, events and optional fields. A name that exists is never removed or renamed. **Why:** a plugin that cannot be updated through the update lane has to be installed by hand, and a paired peer on another version must not break. A change that really cannot be additive bumps the protocol version in `piston_device/proto/__init__.py`, updates the protocol snapshot test in the same commit, and the changelog says that the plugin and the integration must be updated together (see [releasing.md](releasing.md)).

### Device code uses only the standard library
The plugin has no runtime dependencies (`py_modules/` is empty): Decky has no runtime `pip`. Anything that needs a system library (X25519 and ChaCha20-Poly1305 from `libcrypto`, FLAC, Opus) goes through `ctypes`, with a start-up self-test; a library that fails its test is not offered. **Why:** the dependency would not exist on the device.

### Every long-lived component has a synchronous `abort()`
Decky sends the unload request and then stops serving the plugin's event loop, so after the first moment nothing that needs the loop (a timer, a subprocess wait, a goodbye message) can finish, and Decky kills the plugin after five seconds. Everything that must happen on unload therefore runs in `Plugin._unload_now` in `main.py` before its first `await`, and every component that holds a socket, a task, a subprocess or a device exposes a plain (not `async`) `abort()` that it calls. A component that is added later gets an `abort()` and a line in `_unload_now`; the test that lists the components must be extended too. **Why:** helper processes (`gdbus`, `pw-cat`, `systemd-inhibit`) would otherwise outlive the plugin and a half-closed plugin leaves Decky stuck.

### The audio output has one owner
The Sendspin session's audio task is the only code that opens, writes to, adjusts or closes the audio output, with a time limit on every call, a retry backoff while it does not open, and `available=false` reported meanwhile. **Why:** `pw-cat` reads its input on a real-time thread, so while its pipe is empty a volume change hangs; an unbounded call from the message-reading task once froze the whole session.

### Rules for the Steam interface
- Add the media tab by wrapping `tabsHook.render`; never call `tabsHook.add` for it, which makes Decky push its tabs again on every render and corrupts the menu.
- Plugin installs always go through Decky's own confirmation dialog. Never bypass it, and tell the user in Home Assistant that approval is needed.
- Calls to a plugin backend that may be dead need a timeout: they never settle on their own.
- The Steam interface can restart and take Decky's dialog with it; the frontend asks again when it loads.

### Hardware-agnostic wording
Say "device"; do not hard-code a screen size or name a specific model in code or user-facing text. **Why:** the plugin runs on several SteamOS devices.

### Home Assistant conventions
- One entity base class (`entity.py`); a failed device command becomes `ServiceValidationError` when the caller can fix the input and `HomeAssistantError` otherwise (`command_failure`).
- Every platform sets `PARALLEL_UPDATES` explicitly; read-only platforms are unlimited, and the media player must not serialize launches.
- The integration targets the Silver quality scale (`quality_scale.yaml`); the checks that can run without Home Assistant live in `tests/test_quality_rules.py`.
- Home Assistant 2026.10 replaced `voluptuous` with `probatio`; `_compat.py` handles both.
- The integration is tested against small stand-ins for Home Assistant (`tests/ha_stubs`); Home Assistant specific behavior is proven by the real-Home-Assistant suite in `tests/ha`. Tests that run `Plugin._main()` must use `tests.plugin_environment_patches()`, or they start `gdbus` on the host and depend on its desktop.

### The repository is public
No emails, MAC addresses, private IP addresses, personal names or retired project names in the tree; `scripts/check_public.py` (also a test) enforces it. Personal and environment-specific notes belong in the git-ignored `AGENTS.local.md`. Keep the README tables and these docs in step with the behavior; tests check that every entity, action and option appears.

## Common changes

- **Add a long-lived device component** (anything holding a socket, task, subprocess or device): give it a plain `def abort(self)`, call it from `Plugin._unload_now` in `main.py` (directly or through a Plugin-level abort such as `_input_abort`), and add it to `COMPONENTS` in `tests/test_abort_everywhere.py`.
- **Add to the protocol** (command, state group, event, optional field): add it on the device and in the integration; nothing else is needed for the snapshot test, but document it in [protocol.md](protocol.md) and run `python3 scripts/sync_proto.py` if `piston_device/proto/` changed.
- **Make a breaking protocol change** (avoid it): bump `PROTOCOL_VERSION`, edit `tests/protocol_snapshot.json` in the same commit, and say in the changelog that the plugin and the integration must be updated together ([releasing.md](releasing.md)).
- **Add a rule**: add a row to the table in `AGENTS.md` with a reason and a guard (a test id or script path, or `none (convention)`); `tests/test_rules_freshness.py` checks the guard exists.

## Decky Loader facts that shaped the design
- The confirmation call for an install returns nothing even on failure, a wrong hash deletes the installed plugin, and a percent-encoded `file://` path fails; the device therefore verifies the zip itself and keeps a copy to revert to.
- The plugin's Python has no CA certificate bundle: load the system one for HTTPS.
- A suspend can leave the TCP connection open, so standby comes from the `going_to_sleep` event and wake from the `resumed` event (a wall-clock jump), not from a closed connection.
- The session (game or desktop) is detected from process names every 5 seconds.
