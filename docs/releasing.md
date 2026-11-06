# Releasing

1. Bump the version in `package.json` and `custom_components/piston/manifest.json` (a test checks that they match) and add a section for it to `CHANGELOG.md` (a test checks that it exists). When the protocol version changes, the section has to say that the plugin and the integration must be updated together.
2. `pnpm run build`, then `python3 -m unittest discover -s tests -t .`, `python3 scripts/check_public.py` and `python3 scripts/check_release.py v<version>` (it also refuses a release whose dashboard card, `custom_components/piston/www/piston-remote-card.js`, is missing or empty: HACS installs the integration from the tagged tree), and `node --test "tests/js/*.test.js"`.
3. `python3 scripts/package_plugin.py` writes `out/piston-<version>.zip` (one folder named `Piston`, the layout Decky Loader installs) and `out/SHA256SUMS` (`sha256sum` format), which the integration uses to check the zip before the device installs it.
4. Tag `v<version>` and create a GitHub release with the zip and `SHA256SUMS` attached (the tag workflow does this; the release body is the `CHANGELOG.md` section from `scripts/release_notes.py <tag>`, which Home Assistant shows in the update entity, so check it reads well: `python3 scripts/release_notes.py v<version>`). HACS installs the integration from the tag; the plugin zip is installed on the device through Decky Loader.
5. Push the commit and let the HACS and Hassfest workflows finish green **before** tagging (the HACS action is pinned to a commit in `.github/workflows/hacs.yml`; bump it to the current `main` of `hacs/action` first). The release workflow checks both and stops otherwise.

## Staying in the HACS default list

Piston is in the default list (`hacs/default`, file `integration`), which the Home Assistant Marketplace (2026.11) and HACS both read. The requirements are published at https://www.hacs.xyz/docs/publish/include/ and were checked against this repository on 2026-11-05. A test (`tests/test_release_policy.py`) pins the ones that live in files:

- `hacs.json` has `name` and only keys HACS documents; `custom_components/piston/manifest.json` has `domain`, `name`, `version`, `documentation`, `issue_tracker` and `codeowners`; `custom_components/piston/brand/icon.png` exists.
- The HACS workflow (`hacs.yml`) runs `hacs/action` with `category: integration` and passes **without ignores** (no `ignore:` input), and the Hassfest workflow (`hassfest.yml`) runs on every push and pull request. Both also run weekly, so a check that starts failing is noticed. The HACS action is pinned to a commit of its `main`; bump it before each release (https://github.com/hacs/action/commits/main).
- The repository is public, not archived, has an OSI license (BSD-3-Clause), a description, topics and issues enabled. Check with `gh repo view White-Goblin/piston --json description,repositoryTopics,hasIssuesEnabled,isArchived,licenseInfo`.
- HACS takes the integration version from the **tag of the latest full GitHub release** (a bare tag is not enough). The release workflow therefore refuses to publish unless `scripts/check_release.py <tag>` passes (tag = manifest version = `package.json` version, and a `CHANGELOG.md` section exists) and unless HACS validation and Hassfest have a successful push run on the tagged commit. If the guard fails because the checks were still running, re-run the Release workflow once they are green.

What removes a repository from the list: archiving it (the HACS team removes archived repositories automatically), abandoning it, breaking Home Assistant, or deleting it. Do not delete the repository; HACS then refuses to list another one for that account. If it must go, open a removal request first (the "Request for repository removal" issue template in `hacs/integration`). Renaming or transferring the repository needs no pull request: HACS updates its records by itself, but update `manifest.json` links, the README and the `github` remote.

## Plugin self-update and releases

- Home Assistant updates the plugin to the **manifest version of the integration**, using the release asset `piston-<version>.zip` and the `SHA256SUMS` of the tag `v<version>`. A release must therefore be published (not a draft or pre-release) with both files for the integration version to be updatable.
- `FIRST_LANE_VERSION` in `custom_components/piston/const.py` is the first plugin release that can be updated this way (0.4.0). Nothing older is ever targeted.
- The device `hello`, the `challenge`/`proof` exchange, the close codes and the `update` / `update_status` frames are frozen (see [protocol.md](protocol.md#updating-the-plugin)). A test pins them. Changing them means that plugin has to be installed by hand once more.
