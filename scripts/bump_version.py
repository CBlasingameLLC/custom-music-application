"""Set the project version everywhere it lives: python scripts/bump_version.py 0.2.0

Merging the bump to main is what publishes the release (see
.github/workflows/build.yml): CI tags v<version> and attaches the installer.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def set_json_versions(path: Path, version: str, count: int, check) -> None:
    """Rewrite the first `count` top-of-file "version" values in a JSON file, keeping its formatting."""
    original = path.read_text(encoding="utf-8")
    updated, replaced = re.subn(r'("version":\s*")[^"]*(")', rf"\g<1>{version}\g<2>", original, count=count)
    if replaced != count or check(json.loads(updated)) != [version] * count:
        sys.exit(f"could not set the version in {path.relative_to(ROOT)}; edit it by hand")
    path.write_text(updated, encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        sys.exit("usage: python scripts/bump_version.py MAJOR.MINOR.PATCH")
    version = sys.argv[1]

    init_path = ROOT / "src" / "musictoolkit" / "__init__.py"
    init = init_path.read_text(encoding="utf-8")
    init, count = re.subn(r'^__version__ = "[^"]+"', f'__version__ = "{version}"', init, flags=re.MULTILINE)
    if count != 1:
        sys.exit("could not find __version__ in src/musictoolkit/__init__.py")
    init_path.write_text(init, encoding="utf-8")

    # Edit the version lines in place (re-serialising would reflow unrelated lines
    # on every release), then re-read the file to prove the right keys changed.
    package_path = ROOT / "desktop" / "package.json"
    set_json_versions(package_path, version, count=1, check=lambda data: [data["version"]])

    lock_path = ROOT / "desktop" / "package-lock.json"
    if lock_path.exists():
        set_json_versions(
            lock_path, version, count=2, check=lambda data: [data["version"], data["packages"][""]["version"]]
        )

    print(f"version set to {version}")


if __name__ == "__main__":
    main()
