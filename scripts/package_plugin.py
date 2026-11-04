"""Build the Decky plugin zip: python3 scripts/package_plugin.py [output-dir]

Also writes `SHA256SUMS` next to the zip. Run `pnpm run build` first. The zip holds one folder named after the plugin, the layout Decky Loader installs
from ("Install plugin from ZIP" in Decky's developer settings).
"""
import hashlib
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = "Decky Home Assistant"
TOP_FILES = ("main.py", "plugin.json", "package.json", "LICENSE")
TREES = ("dist", "decky_ha", "py_modules")
SKIP_PARTS = {"__pycache__"}
SKIP_SUFFIXES = (".map", ".pyc")


def collect(root: str = ROOT) -> list:
    """(absolute path, path inside the zip) for everything that ships, in a stable order."""
    if not os.path.isfile(os.path.join(root, "dist", "index.js")):
        raise SystemExit("dist/index.js is missing: run `pnpm run build` first")
    files = [(os.path.join(root, f), f) for f in TOP_FILES]
    for tree in TREES:
        for folder, dirs, names in os.walk(os.path.join(root, tree)):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_PARTS)
            for name in sorted(names):
                if not name.endswith(SKIP_SUFFIXES):
                    path = os.path.join(folder, name)
                    files.append((path, os.path.relpath(path, root)))
    return files


def build(out_dir: str, root: str = ROOT) -> str:
    with open(os.path.join(root, "package.json")) as f:
        version = json.load(f)["version"]
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, f"decky-home-assistant-{version}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in collect(root):
            z.write(path, f"{PLUGIN_DIR}/{name}")
    write_checksums(target)
    return target


def write_checksums(zip_path: str) -> str:
    """Write `SHA256SUMS` (sha256sum format) next to the zip; the integration checks downloads against it."""
    with open(zip_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    sums = os.path.join(os.path.dirname(zip_path), "SHA256SUMS")
    with open(sums, "w", encoding="utf-8") as f:
        f.write(f"{digest}  {os.path.basename(zip_path)}\n")
    return sums


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "out")))
