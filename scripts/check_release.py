"""Fail unless a release tag is consistent: python3 scripts/check_release.py v0.7.0

HACS takes the version of an integration from the tag of the latest GitHub release, so the tag, the integration
manifest and package.json must carry the same version, and the CHANGELOG.md must have a section for it (the release
body, which Home Assistant shows in the update entity). Exits 1 and prints every problem.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from release_notes import section  # noqa: E402


def problems(tag: str, root: str = ROOT) -> list:
    found = []
    if not tag.startswith("v"):
        found.append(f"tag {tag!r} does not start with 'v'")
    version = tag.lstrip("v")
    with open(os.path.join(root, "custom_components", "piston", "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)["version"]
    with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
        package = json.load(f)["version"]
    if manifest != version:
        found.append(f"manifest.json version {manifest} is not the tag version {version}")
    if package != version:
        found.append(f"package.json version {package} is not the tag version {version}")
    with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8") as f:
        if section(f.read(), version) is None:
            found.append(f"CHANGELOG.md has no section for {version}")
    return found


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: check_release.py <tag>", file=sys.stderr)
        return 2
    found = problems(argv[1])
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
