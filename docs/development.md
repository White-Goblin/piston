# Development

Layout: `custom_components/piston/` (the integration), `main.py` and `piston_device/` (plugin backend), `src/` (plugin frontend and the picture-in-picture player), `tests/`, `scripts/`.

## Build and test

```
pnpm i && pnpm run build                         # plugin frontend and player (Node 24, pnpm 9)
npx tsc --noEmit -p . | grep -v node_modules     # prints nothing when clean
python3 -m unittest discover -s tests -t .       # all Python tests (standard library only)
python3 scripts/sync_proto.py                    # copy piston_device/proto into the integration
.venv-ha/bin/python -m pytest tests/ha -q        # tests inside a real Home Assistant (dev venv, see tests/ha/README.md)
python3 scripts/check_public.py                  # fail on emails, MACs and private IPs in the tree
```

The integration code is tested against small stand-ins for Home Assistant (`tests/ha_stubs`), so Home Assistant specific behavior is proven by the separate suite in `tests/ha` (a real Home Assistant in a development-only virtualenv, see its README) and on a real instance. The protocol code in `piston_device/proto/` is stdlib only and shared by both sides; a test fails when the integration's copy differs.

`scripts/check_public.py` also reads an optional git-ignored `.public-check.local` with one regular expression per line for your own identifiers (addresses, hostnames), so they cannot be committed by accident.

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

Wording stays hardware agnostic ("device", not a specific model) and nothing hard-codes a screen size. Keep the README entity and action tables in step with the code (a test checks that every entity, action and option name appears).

## Sendspin player

Code in `piston_device/sendspin/`: `crypto.py` (X25519 and ChaCha20-Poly1305 through `ctypes` over the system `libcrypto`, checked with RFC test vectors at start; the plugin has no dependencies), `noise.py` (Noise KKpsk2, checked against transcripts from the `noiseprotocol` package in `tests/sendspin_noise_vectors.py`), `messages.py`, `clock.py` (the specification's time filter), `codecs.py` (FLAC and Opus through `ctypes` over `libFLAC` and `libopus`, each with a start-up self-test; a codec that fails is not offered), `scheduler.py` (queues encoded chunks and decodes them just before they play), `sink.py` (a `pw-cat` stream), `artwork.py` (reassembles the cover from the artwork messages), `card.py` (the media card state), `session.py`, `listener.py` and `mixin.py` (which also exposes the card to the Steam interface: the `sendspin_card` event, the `get_sendspin_card`, `get_sendspin_card_config`, `sendspin_card_command` and `sendspin_card_volume` callables and the nonce-protected `/sendspin/cover` route). The session never touches the audio output from the task that reads the server's messages: the audio task (`_audio_loop` in `session.py`) reconciles the output with what is wanted (open for the current stream, volume and mute, flush, close), with a time limit on every call, a backoff while the output does not open, and `available=false` in `client/state` meanwhile; `FakeSink` can fail or hang to test this, and `Sink.abort()` is the synchronous kill used when the plugin unloads. The plugin's `_unload` does everything in `_unload_now` (`main.py`) synchronously, before its first `await`: Decky closes the plugin runner's socket right after asking for the unload and the loop that reads it then spins without yielding, so nothing that needs the event loop (a timer, a subprocess wait) can finish and Decky kills the plugin after five seconds. Every component therefore has a synchronous `abort()` (`Lifecycle`, `ProtocolServer`, `Advertiser`, the Sendspin listener, session and sink). The frontend side is `src/media/` (`card.ts`, `MediaCard.tsx`, and `registerTab.tsx` which wraps Decky's `tabsHook.render` to add the Quick Access tab; never use `tabsHook.add` for this, it corrupts the menu). The tests use a scripted server (`tests/sendspin_peer.py`) over a real loopback websocket and a fake audio sink, so they need neither PipeWire nor a network. The scripted server can announce any roles and send metadata, controller state and covers (`send_cover`), and the codec tests use real Music Assistant chunks (`tests/sendspin_flac_fixtures.py`, `tests/sendspin_opus_fixtures.py`).

To try it against a real Music Assistant without the device, run the listener from a script on a development machine, or run the reference player with `uvx --from sendspin sendspin daemon` to see what a well-behaved player looks like. Volume and mute set by Music Assistant are saved after two quiet seconds, because announcements raise and restore the volume.
