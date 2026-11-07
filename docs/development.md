# Development

Layout: `custom_components/piston/` (the integration), `main.py` and `piston_device/` (plugin backend), `src/` (plugin frontend and the picture-in-picture player), `tests/`, `scripts/`.

## Build and test

```
pnpm i && pnpm run build                         # plugin frontend and player (Node 24, pnpm 9)
npx tsc --noEmit -p . | grep -v node_modules     # prints nothing when clean
python3 -m unittest discover -s tests -t .       # all Python tests (standard library only)
python3 scripts/sync_proto.py                    # copy piston_device/proto into the integration
.venv-ha/bin/python -m pytest tests/ha -q        # tests inside a real Home Assistant (dev venv, see tests/ha/README.md)
node --test "tests/js/*.test.js"                 # the dashboard card's logic and element (Node 24)
python3 scripts/check_public.py                  # fail on emails, MACs, private IPs and retired project names in the tree
ruff check .                                     # lint (rules in ruff.toml; `pip install ruff==<version in tests.yml>`)
```

The integration code is tested against small stand-ins for Home Assistant (`tests/ha_stubs`), so Home Assistant specific behavior is proven by the separate suite in `tests/ha` (a real Home Assistant in a development-only virtualenv, see its README; CI runs it as the `ha-tests` job) and on a real instance. The protocol code in `piston_device/proto/` is stdlib only and shared by both sides; a test fails when the integration's copy differs.

`scripts/check_public.py` also reads an optional git-ignored `.public-check.local` with one regular expression per line for your own identifiers (addresses, hostnames), so they cannot be committed by accident.

## Rules and their guards

`AGENTS.md` lists the project's rules, each with the test or script that guards it; [architecture.md](architecture.md) gives the reasons. Tests worth knowing when one fails:
- `tests/test_rules_freshness.py`: the guards named in the table exist, the docs name only files that exist, the Current state block of `AGENTS.md` carries the manifest version, and no tracked file mentions the local planning tree.
- `tests/test_device_imports.py`, `tests/test_abort_everywhere.py`, `tests/test_ui_rules.py`, `tests/test_protocol_additive.py` (with `tests/protocol_snapshot.json`): the device and protocol rules.

Keep lint clean with `ruff check .` (lint only, no formatter; the version is pinned in `.github/workflows/tests.yml`).

## Try it on a device

1. Build, then copy `dist`, `main.py`, `piston_device`, `plugin.json`, `package.json`, `LICENSE` and `py_modules` into `~/homebrew/plugins/Piston/` on the device (rsync over SSH) and restart `plugin_loader`.
2. Copy `custom_components/piston` into your Home Assistant's `custom_components` and restart Home Assistant (Python modules are not reloaded otherwise).
3. Pair as described in the README. The plugin log is in `~/homebrew/logs/Piston/`.

The Steam interface can be inspected over the Chrome DevTools protocol on port 8080 of the device (forward it with SSH). Plugin callables can be invoked from the Steam context with `DeckyBackend.call("loader/call_plugin_method", "Piston", "<name>")`.

## Brand art
The icon source is `assets/brand/icon.svg` (a piston rod pushing a play triangle across a TV screen, brass and copper on slate). Render the PNGs with `rsvg-convert` after changing it:

```
rsvg-convert -w 256 -h 256 assets/brand/icon.svg -o custom_components/piston/brand/icon.png
rsvg-convert -w 200 -h 200 assets/brand/icon.svg -o assets/logo.png
```

Do not use Valve's or Home Assistant's own artwork.

## Conventions

The rules and the tests that guard them are listed in `AGENTS.md` (the rules table); the reasons are in [architecture.md](architecture.md). Wording stays hardware agnostic ("device", not a specific model) and nothing hard-codes a screen size. Keep the README entity and action tables in step with the code (a test checks that every entity, action and option name appears).

## Remote card

The card is one hand-written JavaScript file, `custom_components/piston/www/piston-remote-card.js`: no build step and no dependencies, so what is in the repository is what Home Assistant loads. It is served by `card.py`. The pure logic at the top (state table, gestures, wheel, typing, power plan, editor schema) has no DOM and is tested with `node --test "tests/js/*.test.js"`; the element itself is tested against a tiny fake DOM (`tests/js/dom.js`). The test files load the card through `vm` (`tests/js/load.js`) because the repository's `package.json` says `"type": "module"`.

To look at the card in a browser without Home Assistant, `tests/js/harness.html` loads it with a mock `hass` (a fake connection, states and entities): `firefox --headless --screenshot out.png --window-size=390,1000 "file://$PWD/tests/js/harness.html?s=ready"`, where `s` is a status such as `ready`, `device_off` or `desktop` and `mp` the media player state. A browser matters: it caught a start-up error that Node did not.

After changing the card, restart Home Assistant: `card.py` puts the integration version and a fingerprint of the file on the address, so the Home Assistant app loads the new file instead of its cached copy. The real-Home-Assistant tests need the `home-assistant-frontend` package (it is in `tests/ha/requirements.txt`) because the integration depends on the frontend component.

## Sendspin player

Code in `piston_device/sendspin/`:
- `crypto.py`: X25519 and ChaCha20-Poly1305 through `ctypes` over the system `libcrypto`, checked with RFC test vectors at start (the plugin has no dependencies).
- `noise.py`: Noise KKpsk2, checked against transcripts from the `noiseprotocol` package in `tests/sendspin_noise_vectors.py`.
- `messages.py`, `clock.py` (the specification's time filter), `session.py`, `listener.py`.
- `codecs.py`: FLAC and Opus through `ctypes` over `libFLAC` and `libopus`, each with a start-up self-test; a codec that fails is not offered.
- `scheduler.py`: queues encoded chunks and decodes them just before they play.
- `syncfix.py`: the playback sync correction (below).
- `sink.py`: a `pw-cat` stream.
- `artwork.py` (reassembles the cover from the artwork messages) and `card.py` (the media card state).
- `mixin.py`: also exposes the card to the Steam interface: the `sendspin_card` event, the `get_sendspin_card`, `get_sendspin_card_config`, `sendspin_card_command` and `sendspin_card_volume` callables and the nonce-protected `/sendspin/cover` route.

The frontend side is `src/media/`: `card.ts`, `MediaCard.tsx`, and `registerTab.tsx`, which wraps Decky's `tabsHook.render` to add the Quick Access tab (never `tabsHook.add`, see [architecture.md](architecture.md)).

Rules that shape this code are in [architecture.md](architecture.md):
- Unload: everything runs in `_unload_now` (`main.py`) before its first `await`, and every component has a synchronous `abort()` (`Lifecycle`, `ProtocolServer`, `Advertiser`, the Sendspin listener, session and sink). `Sink.abort()` is the synchronous kill used on unload.
- Audio output: the session never touches it from the task that reads the server's messages. The audio task (`_audio_loop` in `session.py`) reconciles the output with what is wanted (open for the current stream, volume and mute, flush, close), with a time limit on every call, a backoff while the output does not open, and `available=false` in `client/state` meanwhile. `FakeSink` can fail or hang to test this.

Playback sync correction (`syncfix.py`, `Scheduler.observe`, `PipeWireSink.pending_us`):
- The clock sync (`client/time`) lines the device clock up with the server's, but the audio hardware runs at its own speed, so a long stream (crossfade keeps one going) drains or fills the pipe to `pw-cat`.
- `PipeWireSink.pending_us()` reads the pipe's byte count (`FIONREAD`, plus asyncio's own buffer). The scheduler compares it with where its model says the next frame plays, and `DriftTracker` keeps a slow average of the difference, relative to the first 15 to 45 seconds of a stream.
- The loop is lopsided on purpose: it inserts single frames from an 8 ms error (it only grows the cushion) and drops them only for a 40 ms lag, because the reading wanders by about 12 ms without any drift (a beat between our writes and the stream's blocks), and a loop that dropped frames on that noise shrank the cushion.
- At most 0.1% of the audio is altered, planned over a minute. The session logs `hardware drift` and `frames trimmed` every 15 seconds when there is something to report and as a heartbeat otherwise.

Tests:
- They use a scripted server (`tests/sendspin_peer.py`) over a real loopback websocket and a fake audio sink, so they need neither PipeWire nor a network.
- The scripted server can announce any roles and send metadata, controller state and covers (`send_cover`).
- The codec tests use real Music Assistant chunks (`tests/sendspin_flac_fixtures.py`, `tests/sendspin_opus_fixtures.py`).
- The sync tests simulate drift and noise (`tests/test_sendspin_syncfix.py`).

Diagnosing a stutter on a device:
- The stream's `ERR` column in `pw-top` is its underrun count.
- The pipe's fill can be sampled from outside by opening `/proc/<pw-cat pid>/fd/0` and calling `FIONREAD`.

To try it against a real Music Assistant without the device, run the listener from a script on a development machine, or run the reference player with `uvx --from sendspin sendspin daemon` to see what a well-behaved player looks like. Volume and mute set by Music Assistant are saved after two quiet seconds, because announcements raise and restore the volume.
