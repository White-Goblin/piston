"""Fail when the tree contains personal or network-specific data that must not be published.

Generic rules always apply (real-looking emails, MAC addresses and private IP addresses). If a git-ignored
`.public-check.local` exists, each of its lines is also a case-insensitive regex for the maintainer's own
identifiers. Run: python3 scripts/check_public.py
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_NAMES = {"pnpm-lock.yaml", ".public-check.local"}
SKIP_SUFFIXES = (".png", ".jpg", ".ico", ".gz", ".zip", ".map")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
EMAIL_OK = {"noreply@anthropic.com"}
MAC = re.compile(r"\b[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5}\b")
MAC_OK = {"aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff", "11:22:33:44:55:66", "00:11:22:33:44:55", "zz:zz:zz:zz:zz:zz"}
IP = re.compile(r"(?<![\d.])((?:10|127|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.]*\d)")
IP_OK_PREFIXES = ("10.0.0.", "192.168.1.", "127.", "172.17.", "10.255.255.255")


def tracked_files() -> list:
    out = subprocess.check_output(["git", "ls-files", "-z", "-c", "-o", "--exclude-standard"], cwd=ROOT)
    return [f for f in out.decode().split("\0") if f and os.path.basename(f) not in SKIP_NAMES and not f.endswith(SKIP_SUFFIXES)]


def local_patterns() -> list:
    path = os.path.join(ROOT, ".public-check.local")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [re.compile(line.strip(), re.IGNORECASE) for line in f if line.strip() and not line.startswith("#")]


def scan(files: list | None = None) -> list:
    """List of 'path:line: reason' findings."""
    patterns = local_patterns()
    findings = []
    for rel in files if files is not None else tracked_files():
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                lines = f.read().split("\n")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for number, line in enumerate(lines, 1):
            for m in EMAIL.finditer(line):
                if m.group(0) not in EMAIL_OK and not m.group(0).startswith("@"):
                    findings.append(f"{rel}:{number}: email address")
            for m in MAC.finditer(line):
                if m.group(0).lower() not in MAC_OK:
                    findings.append(f"{rel}:{number}: MAC address")
            for m in IP.finditer(line):
                if not m.group(1).startswith(IP_OK_PREFIXES):
                    findings.append(f"{rel}:{number}: private IP address {m.group(1)}")
            for pattern in patterns:
                if pattern.search(line):
                    findings.append(f"{rel}:{number}: matches a local identifier")
    return findings


if __name__ == "__main__":
    found = scan()
    for item in found:
        print(item)
    print(f"{len(found)} finding(s)")
    sys.exit(1 if found else 0)
