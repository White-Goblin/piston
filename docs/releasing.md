# Releasing

1. Bump the version in `package.json` and `custom_components/piston/manifest.json` (a test checks that they match) and add a section for it to `CHANGELOG.md` (a test checks that it exists). When the protocol version changes, the section has to say that the plugin and the integration must be updated together.
2. `pnpm run build`, then `python3 -m unittest discover -s tests -t .` and `python3 scripts/check_public.py`.
3. `python3 scripts/package_plugin.py` writes `out/piston-<version>.zip` (one folder named `Piston`, the layout Decky Loader installs) and `out/SHA256SUMS` (`sha256sum` format), which the integration uses to check the zip before the device installs it.
4. Tag `v<version>` and create a GitHub release with the zip and `SHA256SUMS` attached (the tag workflow does this; the release body is the `CHANGELOG.md` section from `scripts/release_notes.py <tag>`, which Home Assistant shows in the update entity, so check it reads well: `python3 scripts/release_notes.py v<version>`). HACS installs the integration from the tag; the plugin zip is installed on the device through Decky Loader.
5. Check that the HACS and Hassfest workflows are green on the tagged commit (the HACS action is pinned to a commit in `.github/workflows/hacs.yml`; bump it to the current `main` of `hacs/action` first).

## Listing in the Marketplace catalog

The Home Assistant Marketplace (2026.11) and HACS read the same catalog, `hacs/default`. Before the pull request that adds `White-Goblin/piston` to its `integration` file: both workflows pass without ignores, a full GitHub release (not only a tag) exists after they passed, its body is the changelog section, the manifest version equals the tag, `hacs.json` has `name` and `homeassistant`, `custom_components/piston/brand/icon.png` exists, and the repository has a description, topics and issues. Open the pull request from the personal account on a new branch from `master`, with the template filled in truthfully and no review requests. Never delete or rename the repository afterwards.

## Plugin self-update and releases

- Home Assistant updates the plugin to the **manifest version of the integration**, using the release asset `piston-<version>.zip` and the `SHA256SUMS` of the tag `v<version>`. A release must therefore be published (not a draft or pre-release) with both files for the integration version to be updatable.
- `FIRST_LANE_VERSION` in `custom_components/piston/const.py` is the first plugin release that can be updated this way (0.4.0). Nothing older is ever targeted.
- The device `hello`, the `challenge`/`proof` exchange, the close codes and the `update` / `update_status` frames are frozen (see [protocol.md](protocol.md#updating-the-plugin)). A test pins them. Changing them means that plugin has to be installed by hand once more.
