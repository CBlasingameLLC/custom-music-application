"""Where a sync puts its files.

A device is either a folder on a drive that has a letter (`LocalFolder`), or a phone or player that has none and
is reached through the MTP helper (`MtpTarget`, in sync/mtp.py). The sync engine in mirror.py only talks to the
small interface below, in device-relative paths with forward slashes ("Artist/Album/01 - Song.mp3").
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from musictoolkit.ingest import moves

logger = logging.getLogger("musictoolkit")

PARTIAL_SUFFIX = ".mtk-partial"
MAX_PATH = 259


class TargetError(OSError):
    """The device would not take (or give back) something. Behaves like any OSError, so the engine treats it the same way."""


@runtime_checkable
class Target(Protocol):
    kind: str  # "folder" | "mtp"
    label: str
    case_sensitive: bool  # may "A.mp3" and "a.mp3" live side by side?
    atomic: bool  # is a copy written under a temporary name and renamed when complete?

    def is_connected(self) -> bool: ...

    def free_space(self) -> int: ...

    def size_of(self, relative: str) -> int | None:
        """The size of the file at this path, or None if there is no such file."""

    def put(self, source: Path, relative: str) -> int:
        """Copy a file onto the device (creating folders) and return the size that arrived."""

    def delete(self, relative: str) -> None:
        """Remove a file from the device, and the folders that leaves empty. Never goes above the device's music folder."""

    def list_names(self, relative_dir: str) -> list[str]:
        """The names (not paths) inside a folder; empty if it does not exist."""

    def write_text(self, relative: str, text: str) -> None: ...

    def path_problem(self, relative: str) -> str | None:
        """Why a file cannot live at this path on this device (too long, ...), or None."""


def as_target(device: Any) -> Target:
    """A folder path (what callers have always passed) or an already-built target."""
    return device if isinstance(device, Target) else LocalFolder(Path(device))


def native(relative: str) -> str:
    """A device path in the form the manifest has always held: whatever separator this machine uses."""
    return relative.replace("\\", "/")


def parent_of(relative: str) -> str:
    return native(relative).rpartition("/")[0]


def join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _copy_file(source: str | Path, dest: Path) -> int:
    """Copy through a temporary name and rename when complete. Returns the size of the finished copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + PARTIAL_SUFFIX)
    try:
        shutil.copyfile(source, partial)
        try:
            shutil.copystat(source, partial)
        except OSError:
            pass  # some file systems refuse to take the date; the copy is still good
        os.replace(partial, dest)
    except BaseException:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    return dest.stat().st_size


def _remove_stale(device_root: Path, relative: str) -> None:
    """Remove a file this app copied earlier, and the folders that leaves empty. Never leaves the device folder."""
    root = Path(device_root)
    target = root.joinpath(*[part for part in native(relative).split("/") if part]).resolve()
    if not target.is_relative_to(root.resolve()):
        logger.warning("Refusing to remove %s: it is outside the device folder", target)
        return
    if target.is_file():
        target.unlink()
        moves.prune_empty_folders(target.parent, root.resolve(), allow_art=True)


class LocalFolder:
    """A folder on a drive: an SD card, a player in drive mode, a USB stick, or any folder at all."""

    kind = "folder"
    atomic = True
    case_sensitive = os.path.normcase("A") == "A"  # False on Windows

    def __init__(self, root: Path, label: str | None = None) -> None:
        self.root = Path(root)
        self.label = label or self.root.name or str(self.root)

    def _abs(self, relative: str) -> Path:
        return self.root.joinpath(*[part for part in native(relative).split("/") if part])

    def is_connected(self) -> bool:
        return self.root.is_dir()

    def free_space(self) -> int:
        return shutil.disk_usage(self.root).free

    def size_of(self, relative: str) -> int | None:
        path = self._abs(relative)
        try:
            return path.stat().st_size if path.is_file() else None
        except OSError:
            return None

    def put(self, source: Path, relative: str) -> int:
        return _copy_file(source, self._abs(relative))

    def delete(self, relative: str) -> None:
        _remove_stale(self.root, relative)

    def list_names(self, relative_dir: str) -> list[str]:
        try:
            return [entry.name for entry in self._abs(relative_dir).iterdir()]
        except OSError:
            return []

    def write_text(self, relative: str, text: str) -> None:
        path = self._abs(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def path_problem(self, relative: str) -> str | None:
        length = len(str(self._abs(relative)))
        return f"The path on the device would be {length} characters" if length > MAX_PATH else None
