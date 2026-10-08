"""Fail unless every place a version number lives agrees; print it for CI.

The source of truth is `__version__` in src/musictoolkit/__init__.py
(pyproject.toml reads it). The desktop package must match, since the
installer's file name and the auto-updater both use it.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read_versions() -> dict[str, str]:
    init = (ROOT / "src" / "musictoolkit" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"\s*$', init, re.MULTILINE)
    if not match:
        sys.exit("could not find __version__ in src/musictoolkit/__init__.py")
    package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    return {
        "src/musictoolkit/__init__.py": match.group(1),
        "desktop/package.json": package["version"],
    }


def main() -> None:
    versions = read_versions()
    for where, version in versions.items():
        if not SEMVER.match(version):
            sys.exit(f"{where}: {version!r} is not MAJOR.MINOR.PATCH")
    if len(set(versions.values())) != 1:
        detail = ", ".join(f"{where}={version}" for where, version in versions.items())
        sys.exit(f"version mismatch: {detail} (use scripts/bump_version.py)")

    version = next(iter(versions.values()))
    print(version)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"version={version}\n")


if __name__ == "__main__":
    main()
