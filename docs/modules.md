# Module map

One line per source file: what it owns, so you can find the right place without reading the tree. Behavior is in [protocol.md](protocol.md) and [reference.md](reference.md); how the pieces fit is in [architecture.md](architecture.md). A test (`tests/test_convention_guards.py::ModuleMapTests`) fails when a source file is missing from this page, so add a line when you add a file.

## Plugin backend (`main.py`, `piston_device/`)
Standard library only (see the device-code rule in [architecture.md](architecture.md)).

- `main.py`: the Decky `Plugin` class. Wires the components together, exposes the callables the frontend uses, and runs `_unload_now` (synchronous teardown that calls every component's `abort()`).
- `server.py`: the authenticated websocket server for the LAN protocol and the command handlers' dispatch.
- `wsproto.py`: minimal RFC 6455 server side over asyncio streams.
- `state.py`: the state groups pushed to the integration, and session (game or desktop) detection.
- `settings.py`: identity, pairing secret and the apps offered as sources, kept in Decky's settings directory.
- `pairing.py`: the pairing state machine (shows the code, runs the SRP exchange, stores the secret).
- `advertise.py`, `mdns.py`: the mDNS advertisement (`mdns.py` is the responder, `advertise.py` keeps the record current).
- `control.py`: device commands (launch and quit apps, power, notifications, sensor interval).
- `input.py`, `uinput.py`, `pointer.py`, `pointer_sync.py`: remote input. `uinput.py` makes the virtual devices, `pointer.py` is the pure pointer model, `pointer_sync.py` resyncs it with the real pointer, `input.py` handles the `input` frames.
- `cursor.py`: the feed behind the cursor overlay drawn by the frontend.
- `media.py`, `media_model.py`, `media_keys.py`, `mpris_meta.py`, `artwork.py`: the `media` group (MPRIS player choice and state, transport keys, metadata parsing, cover art).
- `audio.py`: system volume and mute (HDMI CEC, otherwise PipeWire).
- `sensors.py`, `controllers.py`: system sensors from `/proc` and `/sys`, and controller batteries.
- `lifecycle.py`, `resume.py`, `netwait.py`: sleep and shutdown announcements, the work after a wake, and waiting for the network.
- `wol.py`: Wake on LAN facts and enabling it on the adapter.
- `pip.py`, `pip_signal.py`, `http_server.py`: picture-in-picture commands, signalling between the player page and the integration, and the loopback HTTP server that serves the player.
- `update.py`, `update_check.py`, `update_fetch.py`: plugin self-update (request handling, pure validation, download with checksum and a revert copy).
- `version.py`: the installed plugin version from `package.json`.
- `proto/`: the wire protocol shared with the integration. Edit here only; `scripts/sync_proto.py` copies it. `messages.py` (frames, validation, versions), `auth.py` (mutual HMAC proof), `pake.py` (SRP-6a), `code.py` (pairing code lifecycle), `sensors.py` (sensor descriptions), `__init__.py` (`PROTOCOL_VERSION`).

### Sendspin player (`piston_device/sendspin/`)
- `mixin.py`: the player inside the backend: owns the listener, mDNS record, the `sendspin` state group and the frontend callables.
- `listener.py`, `session.py`: accept the server's connection; one session per connection. `session.py` holds the audio task, the only code that touches the audio output.
- `noise.py`, `crypto.py`: Noise KKpsk2 and the `ctypes` wrappers over `libcrypto`.
- `messages.py`, `artwork.py`, `card.py`: message shapes, cover reassembly, the media card state.
- `codecs.py`: FLAC and Opus through `ctypes`, each with a start-up self-test.
- `clock.py`, `scheduler.py`, `syncfix.py`: time filter, chunk scheduling, and the drift correction.
- `sink.py`: the `pw-cat` output (`Sink` interface, `PipeWireSink`, `FakeSink` for tests).

## Home Assistant integration (`custom_components/piston/`)
- `__init__.py`: setup and unload of a config entry, services.
- `config_flow.py`, `discovery.py`: discovery, pairing, reconfigure; finding a known device again.
- `client.py`, `transport.py`: the LAN protocol client (pure logic) and the aiohttp transport under it.
- `coordinator.py`, `store.py`, `entity.py`: state groups and power state, persisted state, the entity base class.
- Platforms: `media_player.py`, `sensor.py`, `binary_sensor.py`, `switch.py`, `button.py`, `select.py`, `notify.py`, `event.py`, `update.py`. `update_logic.py` holds the update decisions.
- `pip.py`: relays picture-in-picture signalling to a camera entity.
- `artwork.py`: fetches the media image from the device in chunks.
- `input_api.py`, `input_status.py`: the websocket API behind the remote card, and what the card may do right now.
- `card.py`, `www/piston-remote-card.js`: the remote card and the code that serves it (no build step).
- `wol.py`: sends the Wake on LAN packet.
- `unreachable.py`, `diagnostics.py`: the long-unreachable repair issue and diagnostics download.
- `const.py`, `_compat.py`: constants (including `FIRST_LANE_VERSION`) and the voluptuous/probatio shim.
- `proto/`: the copy of the shared protocol. Never edit.
- `manifest.json`, `strings.json`, `translations/en.json`, `services.yaml`, `quality_scale.yaml`: Home Assistant metadata.

## Steam frontend (`src/`)
TypeScript, built by rollup into `dist/`.

- `index.tsx`: plugin entry; registers the panel, overlays, media tab and event handlers.
- `panel/`: the Quick Access panel (`Panel.tsx` and its sections, `usePanelState.ts`, `api.ts` for the backend callables, `types.ts`).
- `media/`: the media card and the Quick Access tab (`registerTab.tsx` wraps `tabsHook.render`; never `tabsHook.add`).
- `cursor/`: the cursor overlay (`CursorOverlay.tsx`, `control.ts`, `cursorPage.ts` is the inlined page).
- `pip/`: the picture-in-picture overlay and store; `useUIComposition.tsx` is adapted from another BSD-3-Clause plugin.
- `player/`: the PiP player page that runs in its own Steam browser view.
- `notify/toasts.tsx`: toasts. `update/install.ts`: plugin install through Decky's own confirmation dialog. `control/device.ts`: app launch and library helpers.
- `types.d.ts`, `player/hls-light.d.ts`: type declarations.

## Scripts (`scripts/`)
`sync_proto.py` (copy the shared protocol), `check_public.py` (public-tree scan), `check_release.py` (tag, version and changelog agree), `package_plugin.py` (the Decky zip and `SHA256SUMS`), `release_notes.py` (the changelog section for a tag).

## Tests (`tests/`)
See [testing.md](testing.md).
