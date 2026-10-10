from __future__ import annotations

import ctypes
import os
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


@dataclass
class VolumeInfo:
    label: str | None = None
    serial: str | None = None  # the volume's serial number: it identifies a drive even if its letter changes
    fs: str | None = None


def volume_info(path: str) -> VolumeInfo:
    """Label, serial number and file system of the volume a path is on. Only Windows can tell; elsewhere (and
    for anything that cannot be asked) every field is None."""
    if platform.system() != "Windows":
        return VolumeInfo()
    try:
        drive = os.path.splitdrive(path)[0]
        if not drive:
            return VolumeInfo()
        label, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
            drive + "\\", label, 261, ctypes.byref(serial), None, None, fs, 261
        )
        if ok:
            return VolumeInfo(label.value or None, f"{serial.value:08X}", fs.value or None)
    except Exception:
        pass
    return VolumeInfo()


def volume_of(path: str) -> DeviceCandidate | None:
    """The mounted volume that holds this path (the deepest mount point that is a prefix of it)."""
    here = os.path.normcase(os.path.normpath(path))
    best: DeviceCandidate | None = None
    for candidate in list_candidate_devices():
        mount = os.path.normcase(os.path.normpath(candidate.mountpoint))
        if here == mount or here.startswith(mount.rstrip(os.sep) + os.sep):
            if best is None or len(candidate.mountpoint) > len(best.mountpoint):
                best = candidate
    return best
