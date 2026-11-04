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
