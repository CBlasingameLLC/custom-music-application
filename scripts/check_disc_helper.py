"""Start the real mtk-disc.exe and check that it speaks the protocol Music Toolkit expects.

    python scripts/check_disc_helper.py helpers/disc/out/mtk-disc.exe

No optical drive is built into a build machine, so this proves what can be proved without one: the program starts and says it is ready, lists
the drives Windows has (usually none), refuses everything that is not the letter of an optical drive before it opens anything, can be told to
stop a request that is under way, and exits when asked. On Windows it then mounts a small disc image, which Windows presents as a real CD/DVD
drive, and checks that the program finds it, reads its contents through the real device controls and puts the tray (the image) away. Whether
reading music works on a given drive can only be checked with that drive and a music disc.

Run it with the Python that has Music Toolkit installed (it uses the app's own client for the helper).
"""

from __future__ import annotations

import os
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

            # Whatever the request says, only the letter of an optical drive may ever lead to a device being opened.
            hostile = ["", "C", "C:\\", "C:\\Windows", "\\\\.\\C:", "\\\\.\\PhysicalDrive0", "\\\\?\\GLOBALROOT\\Device\\CdRom0", "../..", "E:\u0000", "CD:", "1:", "::", "E: "]
            for cmd in ("toc", "read", "flush", "eject"):
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

    if failures:
        sys.exit(f"\n{len(failures)} check(s) failed")
    print("\nthe disc helper is fine", flush=True)


if __name__ == "__main__":
    main()
