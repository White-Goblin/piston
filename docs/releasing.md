# Releasing

1. Bump the version in `package.json` and `custom_components/decky_home_assistant/manifest.json` (a test checks that they match) and add a section for it to `CHANGELOG.md` (a test checks that it exists). When the protocol version changes, the section has to say that the plugin and the integration must be updated together.
2. `pnpm run build`, then `python3 -m unittest discover -s tests -t .` and `python3 scripts/check_public.py`.
3. `python3 scripts/package_plugin.py` writes `out/decky-home-assistant-<version>.zip` (one folder named `Decky Home Assistant`, the layout Decky Loader installs).
4. Tag `v<version>` and create a GitHub release with the zip attached. HACS installs the integration from the tag; the plugin zip is installed on the device through Decky Loader.
5. Check that the HACS and Hassfest workflows are green on the tagged commit.
