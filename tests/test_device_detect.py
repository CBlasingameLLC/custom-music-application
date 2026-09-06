from collections import namedtuple

from musictoolkit.sync import device_detect

_Partition = namedtuple("_Partition", ["device", "mountpoint", "fstype", "opts"])
_Usage = namedtuple("_Usage", ["total", "used", "free", "percent"])


def test_list_candidate_devices_flags_linux_media_mounts(monkeypatch) -> None:
    partitions = [
        _Partition("/dev/sda1", "/", "ext4", "rw"),
        _Partition("/dev/sdb1", "/media/cayl/MP3PLAYER", "vfat", "rw"),
    ]
    monkeypatch.setattr(device_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(device_detect.psutil, "disk_partitions", lambda all=False: partitions)
    monkeypatch.setattr(
        device_detect.psutil, "disk_usage", lambda path: _Usage(total=1_000_000, used=400_000, free=600_000, percent=40.0)
    )

    candidates = device_detect.list_candidate_devices()

    assert len(candidates) == 2
    root = next(c for c in candidates if c.mountpoint == "/")
    removable = next(c for c in candidates if c.mountpoint == "/media/cayl/MP3PLAYER")
    assert root.likely_removable is False
    assert removable.likely_removable is True
    assert removable.free_bytes == 600_000


def test_list_candidate_devices_skips_unreadable_mounts(monkeypatch) -> None:
    partitions = [_Partition("/dev/sdb1", "/media/cayl/GONE", "vfat", "rw")]
    monkeypatch.setattr(device_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(device_detect.psutil, "disk_partitions", lambda all=False: partitions)

    def raise_permission_error(path):
        raise PermissionError("no access")

    monkeypatch.setattr(device_detect.psutil, "disk_usage", raise_permission_error)

    assert device_detect.list_candidate_devices() == []
