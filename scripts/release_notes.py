"""Print the CHANGELOG.md section of a release: python3 scripts/release_notes.py v0.4.2 [changelog-path]

The GitHub release body is shown in Home Assistant's update entity, so it is the changelog section
(not just a compare link). Exits 1 when the version has no section.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def section(changelog: str, version: str) -> str | None:
    """The body of '## <version>' (a title suffix such as '(unreleased)' is allowed), or None."""
    version = version.lstrip("v")
    heading = re.compile(rf"^## {re.escape(version)}(\s.*)?$", re.MULTILINE)
    match = heading.search(changelog)
    if not match:
        return None
    rest = changelog[match.end():]
    following = re.search(r"^## ", rest, re.MULTILINE)
    body = rest[: following.start()] if following else rest
    return body.strip() or None


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: release_notes.py <tag or version> [changelog]", file=sys.stderr)
        return 2
    path = argv[2] if len(argv) > 2 else os.path.join(ROOT, "CHANGELOG.md")
    with open(path, encoding="utf-8") as f:
        body = section(f.read(), argv[1])
    if body is None:
        print(f"CHANGELOG.md has no section for {argv[1]}", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
