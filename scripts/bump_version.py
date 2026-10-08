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

    package_path = ROOT / "desktop" / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["version"] = version
    package_path.write_text(json.dumps(package, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lock_path = ROOT / "desktop" / "package-lock.json"
    if lock_path.exists():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["version"] = version
        if "" in lock.get("packages", {}):
            lock["packages"][""]["version"] = version
        lock_path.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"version set to {version}")


if __name__ == "__main__":
    main()
