"""Start the real mtk-disc.exe and check that it speaks the protocol Music Toolkit expects.

    python scripts/check_disc_helper.py helpers/disc/out/mtk-disc.exe

No optical drive is built into a build machine, so this proves what can be proved without one: the program starts and says it is ready, lists
the drives Windows has (usually none), refuses everything that is not the letter of an optical drive before it opens anything, can be told to
stop a request that is under way, and exits when asked. On Windows it then mounts a small disc image, which Windows presents as a real CD/DVD
drive, and checks that the program finds it, reads its contents through the real device controls and puts the tray (the image) away. Whether
reading music works on a given drive can only be checked with that drive and a music disc.

Burning cannot be tried without a recorder and a blank disc, but the part of Windows' disc burning component (IMAPI) that makes an ISO image
of files needs neither, so on Windows the check makes images, reads them back the way a disc reader would (ISO 9660, Joliet, UDF) and compares
every file with the one it was made from: that proves the files reach IMAPI, that the folders and names come out right, and that a stop works.
What is left for a real recorder is the writing itself.

Run it with the Python that has Music Toolkit installed (it uses the app's own client for the helper).
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures: list[str] = []
MEDIA = {"none", "loading", "audio", "data", "mixed", "unknown"}


def check(ok: bool, message: str) -> None:
    print(("ok    " if ok else "FAIL  ") + message, flush=True)
    if not ok:
        failures.append(message)


def note(message: str) -> None:
    print("note  " + message, flush=True)


# ---------------------------------------------------------------------------------------- a disc image to mount (Windows)


def iso_image(label: str = "MTKCHECK") -> bytes:
    """The smallest ISO 9660 image Windows will mount: a volume descriptor, its terminator, the two path tables and an empty root directory."""

    def both16(n: int) -> bytes:
        return struct.pack("<H", n) + struct.pack(">H", n)

    def both32(n: int) -> bytes:
        return struct.pack("<I", n) + struct.pack(">I", n)

    def padded(text: str, size: int) -> bytes:
        return text.encode("ascii").ljust(size, b" ")

    def directory_record(extent: int, size: int, identifier: bytes) -> bytes:
        length = 33 + len(identifier)
        length += length % 2
        record = struct.pack("<BB", length, 0) + both32(extent) + both32(size)
        record += bytes([126, 1, 1, 0, 0, 0, 0])  # 2026-01-01 00:00:00 UTC (the year is counted from 1900)
        record += struct.pack("<BBB", 2, 0, 0) + both16(1) + struct.pack("<B", len(identifier)) + identifier  # a directory; no unit size, no gap
        return record.ljust(length, b"\0")

    sectors = 22
    root = directory_record(20, 2048, b"\0") + directory_record(20, 2048, b"\1")
    pvd = bytearray(2048)
    pvd[0:7] = b"\x01CD001\x01"
    pvd[8:40] = padded("", 32)
    pvd[40:72] = padded(label, 32)
    pvd[80:88] = both32(sectors)
    pvd[120:124] = both16(1)
    pvd[124:128] = both16(1)
    pvd[128:132] = both16(2048)
    pvd[132:140] = both32(10)  # the path table is 10 bytes long
    pvd[140:144] = struct.pack("<I", 18)  # where the little-endian path table is
    pvd[148:152] = struct.pack(">I", 19)  # and the big-endian one
    pvd[156:190] = root[:34]
    for start, size in ((190, 128), (318, 128), (446, 128), (574, 128)):
        pvd[start : start + size] = b" " * size
    for start in (702, 739, 776):
        pvd[start : start + 37] = b" " * 37
    for start, when in ((813, b"2026010100000000"), (830, b"2026010100000000"), (847, b"0000000000000000"), (864, b"0000000000000000")):
        pvd[start : start + 17] = when + b"\0"  # created, modified, never expires, effective at once
    pvd[881] = 1
    terminator = bytearray(2048)
    terminator[0:7] = b"\xffCD001\x01"
    little = bytearray(2048)
    little[0:10] = struct.pack("<BBIH", 1, 0, 20, 1) + b"\0\0"
    big = bytearray(2048)
    big[0:10] = struct.pack(">BBIH", 1, 0, 20, 1) + b"\0\0"
    directory = bytearray(2048)
    directory[: len(root)] = root
    image = bytearray(2048 * sectors)
    for sector, content in ((16, pvd), (17, terminator), (18, little), (19, big), (20, directory)):
        image[2048 * sector : 2048 * (sector + 1)] = content
    return bytes(image)


def powershell(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=120)


def check_mounted_image(exe: str) -> None:
    """Windows shows a mounted .iso as a CD/DVD drive: the one real device a build machine can have."""
    from musictoolkit.discs.helper import DiscError, DiscHelper

    folder = Path(tempfile.mkdtemp(prefix="mtk-disc-check-"))
    image = folder / "check.iso"
    image.write_bytes(iso_image())
    letter = ""
    try:
        done = powershell(f"$i = Mount-DiskImage -ImagePath '{image}' -PassThru; ($i | Get-Volume).DriveLetter")
        letter = (done.stdout or "").strip()[:1].upper()
        if done.returncode != 0 or not letter.isalpha():
            note(f"a disc image could not be mounted here, so the drive checks are skipped (exit {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]})")
            return
        drive = f"{letter}:"
        note(f"mounted a disc image as {drive}")
        with DiscHelper([exe]) as helper:
            listed = {d["id"]: d for d in helper.call("drives")["drives"]}
            check(drive in listed, f"the mounted image is listed as a CD/DVD drive ({sorted(listed)})")
            if drive not in listed:
                return
            entry = listed[drive]
            print(f"      {entry}", flush=True)
            check(isinstance(entry.get("name"), str) and entry["name"] != "", f"it has a name ({entry.get('name')!r})")
            check(entry.get("media") in MEDIA, f"it says what is in it ({entry.get('media')!r})")
            check(entry.get("media") == "data", "a disc image is a data disc")
            check(entry.get("label") == "MTKCHECK", f"it reads the volume label ({entry.get('label')!r})")
            try:
                contents = helper.call("toc", drive=drive)
                print(f"      {contents}", flush=True)
                check(contents["tracks"] == [{"number": 1, "audio": False, "start": 0}], "its table of contents is one data track from sector 0")
                check(contents["leadout"] > 0, f"the lead-out is after it ({contents['leadout']})")
            except DiscError as error:
                check(False, f"reading the table of contents of a data disc: {error.code}: {error}")
            for cmd, args in (("read", {"lba": 0, "count": 1}), ("flush", {"lba": 0, "count": 1})):
                try:
                    helper.call(cmd, drive=drive, **args)
                    if cmd == "read":
                        check(False, "reading music from a data disc is refused")
                    else:
                        check(True, "flush passes over sectors that cannot be read as music")
                except DiscError as error:
                    note(f"{cmd} on a data disc: {error.code}: {error}")
                    if cmd == "read":
                        check(error.code in ("unsupported", "read_error"), f"reading music from a data disc is refused ({error.code})")
            helper.call("drives")  # still answering after the refusals
            try:
                helper.call("eject", drive=drive)
                note("eject was accepted")
            except DiscError as error:
                note(f"eject on a mounted image: {error.code}: {error}")
    finally:
        if letter:
            powershell(f"Dismount-DiskImage -ImagePath '{image}' | Out-Null")
        for leftover in folder.iterdir():
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------- reading an image the way a disc reader does


class ImageError(Exception):
    pass


def _directory(data: bytes, extent: int, size: int, joliet: bool, prefix: str = "") -> dict[str, tuple[int, int]]:
    """Every file below this directory: {path: (first sector, size)}. A path is as the file system shows it: with Joliet the real names."""
    found: dict[str, tuple[int, int]] = {}
    position, end = extent * 2048, extent * 2048 + size
    while position < end:
        length = data[position]
        if length == 0:  # the rest of this sector is padding
            position = (position // 2048 + 1) * 2048
            continue
        first = struct.unpack_from("<I", data, position + 2)[0]
        size_of = struct.unpack_from("<I", data, position + 10)[0]
        flags = data[position + 25]
        raw = data[position + 33 : position + 33 + data[position + 32]]
        position += length
        if raw in (b"\x00", b"\x01"):  # "." and ".."
            continue
        name = re.sub(r";\d+$", "", raw.decode("utf-16-be" if joliet else "ascii"))  # (the version number a name may end with: ";1")
        if not joliet:
            name = name.rstrip(".")
        if flags & 2:
            found.update(_directory(data, first, size_of, joliet, prefix + name + "/"))
        else:
            found[prefix + name] = (first, size_of)
    return found


def read_image(path: Path) -> dict:
    """What an ISO image file says it holds: its volume names, which file systems it has and the files of the ISO 9660 and Joliet trees (with
    their contents, so that they can be compared). Raises ImageError for an image no reader would accept."""
    data = path.read_bytes()
    if len(data) % 2048 or len(data) < 18 * 2048:
        raise ImageError(f"the image is {len(data)} bytes, which is not a whole number of sectors (or too small)")
    primary = joliet = None
    sector = 16
    while sector * 2048 < len(data):
        block = data[sector * 2048 : (sector + 1) * 2048]
        if block[1:6] != b"CD001":
            break
        if block[0] == 1 and primary is None:
            primary = block
        elif block[0] == 2 and block[88:91] in (b"%/@", b"%/C", b"%/E"):
            joliet = block
        elif block[0] == 255:
            break
        sector += 1
    image: dict = {"sectors": len(data) // 2048, "iso9660": None, "joliet": None, "udf": False}
    # UDF announces itself in the volume recognition sequence after the ISO descriptors (or on its own, when it is the only file system): "NSR02" or "NSR03"
    image["udf"] = any(data[at * 2048 + 1 : at * 2048 + 6] in (b"NSR02", b"NSR03") for at in range(16, 40) if (at + 1) * 2048 <= len(data))
    for key, volume, is_joliet in (("iso9660", primary, False), ("joliet", joliet, True)):
        if volume is None:
            continue
        root = volume[156:190]
        first, size = struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0]
        name = volume[40:72]
        label = name.decode("utf-16-be" if is_joliet else "ascii").strip()
        tree = _directory(data, first, size, is_joliet)
        image[key] = {"label": label, "files": {p: data[a * 2048 : a * 2048 + n] for p, (a, n) in tree.items()}}
    return image


def check_image_against(label: str, image: dict, expected: dict[str, bytes], systems: tuple[str, ...]) -> None:
    for system in systems:
        if system == "udf":
            check(image["udf"], f"{label}: it has a UDF file system")
            continue
        found = image.get(system)
        check(found is not None, f"{label}: it has an {system} file system")
        if found is None:
            continue
        files = found["files"]
        if system == "joliet":
            check(files == expected, f"{label}: the Joliet tree holds exactly the files that were asked for, with their contents ({sorted(files)[:6]})")
        else:
            check(sorted(len(v) for v in files.values()) == sorted(len(v) for v in expected.values()) and sorted(files.values()) == sorted(expected.values()),
                  f"{label}: the ISO 9660 tree holds the same contents ({len(files)} files)")


def check_images(exe: str) -> None:
    """Windows' disc burning component can make an image without a recorder: the one thing about burning a build machine can prove."""
    from musictoolkit.discs.helper import DiscError, DiscHelper

    folder = Path(tempfile.mkdtemp(prefix="mtk-iso-check-"))
    try:
        sources = folder / "sources"
        sources.mkdir()
        content = {
            "Artist/Album/01 - First Song.mp3": os.urandom(5000),
            "Artist/Album/02 - Second \u00e9\u00e8 Song.flac": os.urandom(2048 * 3 + 17),
            "Artist/Other Album/01 - Third.ogg": os.urandom(1),
            "Loose Song.mp3": os.urandom(70000),
        }
        files = []
        for dest, data in content.items():
            source = sources / dest.replace("/", "_")
            source.write_bytes(data)
            files.append({"src": str(source), "dest": dest})
        with DiscHelper([exe]) as helper:
            probe = helper.call("imapi_probe")
            print(f"      {probe}", flush=True)
            components = probe.get("components", {})
            if not components or not all(v is True for v in components.values()):
                note("Windows' disc burning component is not all here, so the image checks are skipped")
                return
            check(True, "every IMAPI component can be made")
            out = folder / "check.iso"
            reply = helper.call("build_iso", label="MTK Test", label_iso="MTK_TEST", filesystems=["iso9660", "joliet"], files=files, out=str(out))
            check(out.is_file() and reply.get("bytes") == out.stat().st_size and out.stat().st_size % 2048 == 0, f"an image of files is made ({reply})")
            check(not Path(str(out) + ".part").exists(), "and no half-written file is left beside it")
            try:
                image = read_image(out)
                check_image_against("ISO 9660 + Joliet", image, {k: v for k, v in content.items()}, ("iso9660", "joliet"))
                check(image["joliet"] is not None and image["joliet"]["label"] == "MTK Test", f"Joliet carries the name of the disc ({image['joliet'] and image['joliet']['label']!r})")
                named = image["iso9660"] is not None and re.sub(r"[^A-Z0-9]", "_", image["iso9660"]["label"].upper()) == "MTK_TEST"  # (however the component spells the separator)
                check(named, f"ISO 9660 carries the name of the disc ({image['iso9660'] and image['iso9660']['label']!r})")
            except ImageError as error:
                check(False, f"reading the image back: {error}")

            before = out.read_bytes()
            try:
                helper.call("build_iso", label="MTK Test", label_iso="MTK_TEST", filesystems=["iso9660", "joliet"], files=files, out=str(out))
                check(False, "an image never replaces a file that is there")
            except DiscError as error:
                check(error.code == "failed" and out.read_bytes() == before, f"an image never replaces a file that is there ({error.code}: {error})")

            udf = folder / "udf.iso"
            helper.call("build_iso", label="MTK UDF", label_iso="MTK_UDF", filesystems=["udf"], files=files, out=str(udf))
            try:
                check_image_against("UDF", read_image(udf), content, ("udf",))
            except ImageError as error:
                check(udf.is_file() and udf.stat().st_size % 2048 == 0, f"the UDF image is whole sectors ({error})")

            for bad, why in ((folder / "no-extension", "a name that is not .iso"), (folder / "missing" / "x.iso", "a folder that is not there"),
                             ("relative.iso", "a relative path"), ("\\\\.\\PhysicalDrive0\\x.iso", "a device path")):
                try:
                    helper.call("build_iso", label="X", label_iso="X", filesystems=["iso9660"], files=files, out=str(bad))
                    check(False, f"an image is not written to {why}")
                except DiscError as error:
                    check(error.code == "failed", f"an image is not written to {why} ({error.code})")
            for dest in ("../escape.mp3", "a/../b.mp3", "C:/x.mp3", "a\\b.mp3", "", "/"):
                try:
                    helper.call("build_iso", label="X", label_iso="X", filesystems=["iso9660"], files=[{"src": files[0]["src"], "dest": dest}], out=str(folder / "bad.iso"))
                    check(False, f"a place on the disc like {dest!r} is refused")
                except DiscError as error:
                    check(error.code == "failed" and not (folder / "bad.iso").exists(), f"a place on the disc like {dest!r} is refused ({error.code})")
            try:
                helper.call("build_iso", label="X", label_iso="X", filesystems=["iso9660"], files=[{"src": str(folder / "gone.mp3"), "dest": "a.mp3"}], out=str(folder / "gone.iso"))
                check(False, "a file that is not there is refused")
            except DiscError as error:
                check(error.code == "failed" and not (folder / "gone.iso").exists(), f"a file that is not there is refused ({error.code})")

            big = folder / "big.bin"
            with open(big, "wb") as handle:
                handle.truncate(1024**3)  # a gigabyte of nothing: a stop that arrives as soon as the image is being written out has plenty of it left to stop

            def build_big_and_stop(once_writing: bool) -> tuple[DiscError | None, float, float | None, int]:
                """Asks for the image of the big file and sends a stop: as soon as the helper reports (it is writing the image out by then), or 0.4 s
                after the request, wherever it is by then. Gives the refusal (None if the image was made), the seconds it took, the seconds from the
                stop to the answer, and the number of reports heard."""
                reports: list[dict] = []
                writing = threading.Event()  # set by the first report, and again when the request is over
                over = threading.Event()
                sent: list[float] = []

                def heard(event: dict) -> None:
                    reports.append(event)
                    writing.set()

                def stop() -> None:
                    if once_writing:
                        writing.wait(60)  # (an image that never reports is stopped after a minute)
                    else:
                        time.sleep(0.4)
                    if not over.is_set():  # (a stop must never reach a request that came after this one)
                        sent.append(time.monotonic())
                        helper.cancel()

                began = time.monotonic()
                threading.Thread(target=stop, daemon=True).start()
                refusal = None
                try:
                    helper.call("build_iso", label="BIG", label_iso="BIG", filesystems=["iso9660", "joliet", "udf"], files=[{"src": str(big), "dest": "big.bin"}],
                                out=str(folder / "big.iso"), on_event=heard)
                except DiscError as error:
                    refusal = error
                finally:
                    over.set()
                    writing.set()
                ended = time.monotonic()
                return refusal, ended - began, (ended - sent[0]) if sent else None, len(reports)

            def trouble(refusal: DiscError | None, reports: int) -> str:
                said = " | ".join(getattr(helper, "_stderr", ()))[-700:]  # what the helper printed itself: a slip in it shows here, with its stack
                what = f"{refusal.code}: {refusal}" if refusal else "the image was made"
                return f"{what}; {reports} reports; the helper said: {said or 'nothing'}"

            # a stop that arrives while the image is being written out
            refusal, took, late, reports = build_big_and_stop(True)
            stopped = refusal is not None and refusal.code == "cancelled"
            check(stopped, f"an image that is told to stop while it is being written stops ({'cancelled' if stopped else trouble(refusal, reports)}; {took:.1f} s, "
                           f"{'-' if late is None else f'{late:.1f}'} s after the stop)")
            check(not (folder / "big.iso").exists() and not (folder / "big.iso.part").exists(), "a stopped image leaves no file behind")
            for leftover in (folder / "big.iso", folder / "big.iso.part"):
                leftover.unlink(missing_ok=True)

            # a stop that arrives 0.4 s in is wherever the helper is by then (taking the files in, laying the image out, writing it out). It stops
            # wherever it can; a failure here is reported, and what it said is kept in the log, but is not held against the build until it is understood
            refusal, took, late, reports = build_big_and_stop(False)
            if refusal is not None and refusal.code == "cancelled":
                check(True, f"an image that is told to stop 0.4 s in stops ({took:.1f} s)")
            else:
                note(f"an image that is told to stop 0.4 s in did not stop ({trouble(refusal, reports)}; {took:.1f} s)")
            check(not (folder / "big.iso.part").exists(), "and leaves no half-written file behind")
            check(helper.call("drives") is not None, "and the helper still answers")
    except DiscError as error:
        check(False, f"making images: {error.code}: {error}")
    finally:
        for leftover in sorted(folder.rglob("*"), reverse=True):
            try:
                leftover.unlink() if leftover.is_file() else leftover.rmdir()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------- a music CD without gaps: the image of the whole disc


def raw_song(seed: int, sectors: int) -> bytes:
    """Sectors of CD audio no two of which are alike, so that each can be found in an image by its bytes."""
    import random

    return random.Random(seed).randbytes(2352 * sectors)


def crc16(data: bytes) -> int:
    """The check value the Q sub-channel ends with (CCITT, stored inverted)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF


def q_channel(sub: bytes) -> dict | None:
    """The Q sub-channel out of 96 bytes of sub-code (P is bit 7 of each byte, Q bit 6): where on the disc this sector says it is."""
    if len(sub) < 96:
        return None
    bits = [(b >> 6) & 1 for b in sub[:96]]
    q = bytes(sum(bit << (7 - k) for k, bit in enumerate(bits[8 * i : 8 * i + 8])) for i in range(12))

    def bcd(x: int) -> int:
        return (x >> 4) * 10 + (x & 15)

    return {"track": bcd(q[1]), "index": bcd(q[2]), "relative": (bcd(q[3]), bcd(q[4]), bcd(q[5])), "absolute": (bcd(q[7]), bcd(q[8]), bcd(q[9])),
            "crc": crc16(q[:10]) == (q[10] << 8 | q[11])}


def look_in_image(image: bytes, songs: list[bytes]) -> dict:
    """Where the songs are in an image of a disc: the size of a sector of the image (the distance between two sectors of the first song), the
    sector of the image each song begins at, whether every sector of every song is there word for word, and the distance between the end of one
    song and the start of the next in sectors."""
    first = image.find(songs[0][:2352])
    if first < 0:
        return {"found": False}
    second = image.find(songs[0][2352:4704], first)
    stride = second - first if second > first else 0
    found: dict = {"found": True, "stride": stride, "first_offset": first, "image_sectors": len(image) / stride if stride else None}
    if not stride:
        return found
    starts = []
    intact = True
    for number, song in enumerate(songs):
        at = image.find(song[:2352], first if number == 0 else starts[-1] * stride + first)
        starts.append((at - first) // stride if at >= 0 and (at - first) % stride == 0 else -1)
        if starts[-1] < 0:
            intact = False
            continue
        for sector in range(len(song) // 2352):
            offset = first + (starts[-1] + sector) * stride
            if image[offset : offset + 2352] != song[sector * 2352 : (sector + 1) * 2352]:
                intact = False
                found["first_difference"] = (number + 1, sector)
                break
    found["starts"] = starts
    found["intact"] = intact
    found["between"] = [starts[i + 1] - starts[i] - len(songs[i]) // 2352 for i in range(len(songs) - 1)] if all(x >= 0 for x in starts) else None
    if stride >= 2352 + 96:
        around = []
        if all(x >= 0 for x in starts):
            for label, sector in (("last sector of song 1", starts[1] - 1), ("first sector of song 2", starts[1]), ("150 sectors before song 2", starts[1] - 150)):
                at = first + sector * stride + 2352
                around.append((label, q_channel(image[at : at + 96])))
        found["q_channel"] = around
    return found


def check_raw_images(exe: str) -> None:
    """Windows' disc burning component can lay songs out as the image of a whole disc without a recorder. That is the part of a burn without gaps
    that a build machine can prove: that the songs reach IMAPI, that they come out in the image word for word, and that nothing lies between them."""
    from musictoolkit.discs.helper import DiscError, DiscHelper

    folder = Path(tempfile.mkdtemp(prefix="mtk-raw-check-"))
    try:
        lengths = (320, 400, 310)
        songs = [raw_song(number, sectors) for number, sectors in enumerate(lengths, start=1)]
        tracks = []
        for number, data in enumerate(songs, start=1):
            path = folder / f"song{number}.pcm"
            path.write_bytes(data)
            tracks.append({"path": str(path), "sectors": len(data) // 2352})
        with DiscHelper([exe]) as helper:
            probe = helper.call("imapi_probe")
            raws = probe.get("raw_components", {})
            print(f"      {raws}", flush=True)
            if not raws or not all(v is True for v in raws.values()):
                note("the components that write a disc in one go are not all here, so the checks of a music CD without gaps are skipped")
                return
            check(True, "the components that write a disc in one go can be made")
            laid = {}
            for sector_type in (1, 2, 3):
                for prop in (True, False):
                    try:
                        facts = helper.call("raw_image", tracks=tracks, type=sector_type, property=prop, timeout=300)
                        laid[(sector_type, prop)] = facts
                        print(f"      type {sector_type}, property {prop}: {facts}", flush=True)
                    except DiscError as error:
                        print(f"      type {sector_type}, property {prop}: {error.code}: {error}", flush=True)
            check(bool(laid), f"songs can be laid out as the image of a disc ({len(laid)} of 6 ways)")
            if not laid:
                return
            seamless = [(k, v) for k, v in laid.items() if v.get("seamless")]
            check(bool(seamless), f"and one of the settings lays them out without gaps ({[(k, v.get('leadout')) for k, v in laid.items()]})")
            for sector_type in (1, 2, 3):
                pair = [laid.get((sector_type, True)), laid.get((sector_type, False))]
                if all(pair):
                    short = [p["leadout"] - p["starts"][0] for p in pair]
                    note(f"type {sector_type}: the disc is {short[0]} sectors long with true and {short[1]} with false (the songs add up to {sum(lengths)})")
            if not seamless:
                return
            (sector_type, prop), facts = seamless[0]
            out = folder / "seamless.bin"
            made = helper.call("raw_image", tracks=tracks, type=sector_type, property=prop, out=str(out), timeout=300)
            image = out.read_bytes()
            check(len(image) == made.get("written"), f"the image is read out whole ({len(image)} bytes)")
            if made.get("bytes") is not None:
                check(made["bytes"] == len(image), f"and is as long as the stream says ({made['bytes']})")
            seen = look_in_image(image, songs)
            print(f"      {seen}", flush=True)
            check(seen.get("found") is True, "the first song is in the image")
            if seen.get("found"):
                check(bool(seen.get("stride")) and seen["stride"] >= 2352, f"and the image counts its sectors in {seen.get('stride')} bytes")
                check(seen.get("intact") is True, "every sector of every song is in the image, word for word")
                check(seen.get("between") == [0, 0], f"and the songs run on from one another: nothing lies between them ({seen.get('between')})")
            out.unlink(missing_ok=True)

            other = laid.get((sector_type, not prop))
            if other:
                again = helper.call("raw_image", tracks=tracks, type=sector_type, property=not prop, out=str(out), timeout=300)
                seen = look_in_image(out.read_bytes(), songs)
                note(f"the other setting: {seen}")
                out.unlink(missing_ok=True)
                check(again.get("seamless") is False, "the other setting leaves the usual two seconds between the songs")
    except DiscError as error:
        check(False, f"laying songs out as a disc: {error.code}: {error}")
    finally:
        for leftover in sorted(folder.rglob("*"), reverse=True):
            try:
                leftover.unlink() if leftover.is_file() else leftover.rmdir()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------- the checks


def check_stops_arriving_early_and_late(exe: str) -> None:
    """A request to stop can arrive before the request it is about has begun, or after it has been answered. The first must stop it; the
    second must not stop the next one."""
    import json

    process = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, "MTK_DISC_TEST": "1"})

    def send(message: dict) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def reply_to(number: int) -> dict:
        while True:
            line = process.stdout.readline()
            if not line:
                return {}
            message = json.loads(line)
            if message.get("id") == number:
                return message

    try:
        process.stdout.readline()  # ready
        send({"id": 1, "cmd": "wait", "ms": 10})
        check(reply_to(1).get("ok") is True, "a short request is answered")
        send({"cmd": "cancel", "target": 1})  # too late: 1 has been answered
        send({"id": 2, "cmd": "wait", "ms": 100})
        check(reply_to(2).get("ok") is True, "a stop that arrives after the answer does not stop the next request")
        send({"cmd": "cancel", "target": 3})  # too early: 3 has not begun
        started = time.monotonic()
        send({"id": 3, "cmd": "wait", "ms": 20000})
        answer = reply_to(3)
        check(answer.get("code") == "cancelled" and time.monotonic() - started < 5, f"a stop that arrives before the request begins stops it ({answer.get('code')})")
        send({"id": 4, "cmd": "wait", "ms": 10})
        check(reply_to(4).get("ok") is True, "and the request after that is not stopped")
        send({"id": 5, "cmd": "close"})
        reply_to(5)
        process.wait(timeout=20)
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        check(False, f"stops that arrive early or late: {error}")
        process.kill()
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    exe = str(Path(sys.argv[1]).resolve())
    if not Path(exe).is_file():
        sys.exit(f"{exe} does not exist")

    import json
    import re

    from musictoolkit.discs.helper import DiscError, DiscHelper, DiscUnavailable

    done = subprocess.run([exe, "--selftest"], capture_output=True, text=True, timeout=120)
    print(f"--selftest: exit {done.returncode}, stdout {done.stdout.strip()!r}, stderr {done.stderr.strip()[-400:]!r}", flush=True)
    selftest = {}
    try:
        selftest = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass
    check(done.returncode == 0 and selftest.get("ok") is True, "--selftest asks Windows what it has")
    check(selftest.get("helper") == "mtk-disc" and selftest.get("version") == 1, "it speaks protocol version 1")
    check(isinstance(selftest.get("drives"), int), f"it counts the optical drives Windows lists ({selftest.get('drives')})")
    check(selftest.get("request_size") == 16, f"the structure a raw read is asked with is 16 bytes ({selftest.get('request_size')})")

    os.environ["MTK_DISC_TEST"] = "1"  # switches on the commands that only these checks use (they open no drive)
    try:
        with DiscHelper([exe]) as helper:
            check(True, "it starts and says it is ready")
            drives = helper.call("drives")["drives"]
            shaped = isinstance(drives, list) and all(
                isinstance(d, dict) and re.fullmatch(r"[A-Z]:", str(d.get("id"))) and isinstance(d.get("name"), str) and d.get("media") in MEDIA
                and isinstance(d.get("label"), str)
                for d in drives
            )
            check(shaped, f"every drive it lists has a letter, a name, a state and a label ({drives})")
            check(
                all(isinstance(d.get("writer"), bool) and isinstance(d.get("writes"), list) and (d["writer"] or d["writes"] == []) for d in drives),
                f"and says whether it writes discs, and which kinds ({[(d['id'], d.get('writer'), d.get('writes')) for d in drives]})",
            )

            # Whatever the request says, only the letter of an optical drive may ever lead to a device being opened.
            hostile = ["", "C", "C:\\", "C:\\Windows", "\\\\.\\C:", "\\\\.\\PhysicalDrive0", "\\\\?\\GLOBALROOT\\Device\\CdRom0", "../..", "E:\u0000", "CD:", "1:", "::", "E: "]
            for cmd in ("toc", "read", "flush", "eject", "media"):
                refused = []
                for name in hostile:
                    try:
                        helper.call(cmd, drive=name, lba=0, count=1)
                    except DiscError as error:
                        refused.append(error.code)
                    else:
                        refused.append("ACCEPTED")
                accepted = [n for n, code in zip(hostile, refused) if code == "ACCEPTED" and n.strip() != "E:"]
                check(not accepted, f"{cmd}: nothing but a drive letter is accepted (accepted: {accepted})")
            try:
                helper.call("toc", drive="\\\\.\\PhysicalDrive0")
            except DiscError as error:
                check(error.code == "not_found", f"a device path is refused as not a drive letter ({error.code}: {error})")

            if sys.platform == "win32":
                import ctypes

                mask = ctypes.windll.kernel32.GetLogicalDrives()
                present = [f"{chr(65 + i)}:" for i in range(26) if mask & (1 << i)]
                absent = next((f"{chr(65 + i)}:" for i in range(25, 1, -1) if not mask & (1 << i)), None)
                optical = {d["id"] for d in drives}
                fixed = next((p for p in present if p not in optical), None)
                if fixed:
                    try:
                        helper.call("toc", drive=fixed)
                        check(False, f"{fixed} is not an optical drive and is refused")
                    except DiscError as error:
                        check(error.code == "not_optical", f"{fixed} is not an optical drive and is refused as such ({error.code})")
                if absent:
                    try:
                        helper.call("toc", drive=absent)
                        check(False, f"{absent} does not exist and is refused")
                    except DiscError as error:
                        check(error.code == "not_found", f"{absent} does not exist and is refused as such ({error.code})")

            writing = [name for name in hostile if name.strip() != "E:"]  # (never a real drive's letter: these would write to it)
            for cmd, args in (("burn_audio", {"tracks": []}), ("burn_data", {"files": [], "label": "X"}), ("erase", {"full": False})):
                refused = []
                for name in writing:
                    try:
                        helper.call(cmd, drive=name, **args)
                        refused.append("ACCEPTED")
                    except DiscError as error:
                        refused.append(error.code)
                check("ACCEPTED" not in refused, f"{cmd}: nothing but a drive letter is accepted ({sorted(set(refused))})")
            for name in ("C", "\\\\.\\PhysicalDrive0", "../..", "::"):
                for cmd in ("burn_audio", "burn_data", "erase"):
                    try:
                        helper.call(cmd, drive=name, tracks=[], files=[], label="X")
                    except DiscError as error:
                        if cmd != "burn_audio" and cmd != "burn_data":
                            check(error.code == "not_found", f"{cmd}: {name!r} is refused as not a drive letter ({error.code})")

            for cmd, args in (("nonsense", {}), ("read", {"drive": "E:", "lba": -1, "count": 1}), ("read", {"drive": "E:", "lba": 0, "count": 99}),
                              ("read", {"drive": "E:", "lba": 0, "count": 0}), ("flush", {"drive": "E:", "lba": 0, "count": 100000})):
                try:
                    helper.call(cmd, **args)
                    check(False, f"{cmd} {args} is refused")
                except DiscError as error:
                    check(error.code == "failed", f"{cmd} {args} is refused ({error.code}: {error})")

            events: list[dict] = []
            helper.call("ticks", n=4, on_event=events.append)
            check([e.get("done") for e in events] == [1, 2, 3, 4] and all(e.get("total") == 4 for e in events), f"a long request reports how far it is ({len(events)} events)")

            started = time.monotonic()
            threading.Timer(0.3, helper.cancel).start()
            try:
                helper.call("wait", ms=20000)
                check(False, "a request that is told to stop stops")
            except DiscError as error:
                waited = time.monotonic() - started
                check(error.code == "cancelled" and waited < 5, f"a request that is told to stop stops: {error.code} after {waited:.1f} s")
            before = helper.call("drives")["drives"]
            check(before == drives, "it still answers after being told to stop")
    except DiscUnavailable as error:
        check(False, f"talking to the helper: {error}")
    except DiscError as error:
        check(False, f"talking to the helper: {error.code}: {error}")

    check_stops_arriving_early_and_late(exe)

    process = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready = process.stdout.readline()
        process.stdin.write('{"id": 1, "cmd": "close"}\n')
        process.stdin.flush()
        process.stdout.readline()
        code = process.wait(timeout=20)
        check('"ready"' in ready and code == 0, f"it exits when asked to close (exit {code})")
    except (subprocess.TimeoutExpired, OSError) as error:
        process.kill()
        check(False, f"it exits when asked to close: {error}")
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    process = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        process.stdout.readline()
        process.stdin.close()
        code = process.wait(timeout=20)
        check(code == 0, f"it exits when the app that started it goes away (exit {code})")
    except subprocess.TimeoutExpired:
        process.kill()
        check(False, "it exits when the app that started it goes away")
    finally:
        for stream in (process.stdout, process.stderr):
            stream.close()

    if sys.platform == "win32":
        check_mounted_image(exe)
        check_images(exe)
        check_raw_images(exe)

    if failures:
        sys.exit(f"\n{len(failures)} check(s) failed")
    print("\nthe disc helper is fine", flush=True)


if __name__ == "__main__":
    main()
