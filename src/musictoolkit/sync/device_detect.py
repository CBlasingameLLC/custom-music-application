from __future__ import annotations

import ctypes
import platform
from dataclasses import dataclass

import psutil

_LINUX_REMOVABLE_PREFIXES = ("/media/", "/run/media/", "/mnt/")
_MACOS_REMOVABLE_PREFIX = "/Volumes/"
_WINDOWS_DRIVE_REMOVABLE = 2


@dataclass
class DeviceCandidate:
    mountpoint: str
    device: str
    fstype: str
    total_bytes: int
    free_bytes: int
    likely_removable: bool


def _is_likely_removable(mountpoint: str) -> bool:
    system = platform.system()
    if system == "Linux":
        return mountpoint.startswith(_LINUX_REMOVABLE_PREFIXES)
    if system == "Darwin":
        return mountpoint.startswith(_MACOS_REMOVABLE_PREFIX)
    if system == "Windows":
        try:
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(mountpoint))  # type: ignore[attr-defined]
            return drive_type == _WINDOWS_DRIVE_REMOVABLE
        except Exception:
            return False
    return False


def list_candidate_devices() -> list[DeviceCandidate]:
    """Every mounted volume with capacity info, flagged with a best-effort
    'likely_removable' heuristic. The full list is always returned — even a
    perfect heuristic shouldn't auto-select the target for a prune-capable
    sync, so the caller always requires an explicit, manually-confirmed
    mount path."""
    candidates = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (OSError, PermissionError):
            continue
        candidates.append(
            DeviceCandidate(
                mountpoint=part.mountpoint,
                device=part.device,
                fstype=part.fstype,
                total_bytes=usage.total,
                free_bytes=usage.free,
                likely_removable=_is_likely_removable(part.mountpoint),
            )
        )
    return candidates
