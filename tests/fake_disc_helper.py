"""A stand-in for mtk-disc.exe that speaks the same JSON lines but keeps its drives and discs in plain folders.

    python tests/fake_disc_helper.py <root>          run it (what MTK_DISC_HELPER points at)
    python tests/fake_disc_helper.py --make <root>   make a small world to run it in: drive E: with a 2-track audio disc, drive F: empty

<root>/<letter>/drive.json   {"name": "Stand-in CD/DVD drive", "writer": false, "writes": ["CD-R", "CD-RW"]}     one folder per drive (E, F, ...)
<root>/<letter>/disc.json    present while a disc is in the drive:
                             {"kind": "audio" | "data" | "blank" | "unknown", "label": "...", "seed": 1,
                              "tracks": [{"number": 1, "audio": true, "sectors": 300}, ...]}     (laid out one after the other from sector 0,
                             unless a track has a "start"; "pcm": "disc.pcm" holds the audio of a disc that was burned here)
                             a blank or burned disc also has "type" ("CD-R", "CD-RW", ...), "rewritable" and "capacity" (sectors)
<root>/control.json          optional, read before every command, to make things go wrong:
    {"unplugged": ["F"],                              the drive is gone
     "noise": true,                                   stray output between replies (or a list of the lines to print)
     "lowercase": true,                               the listing gives drive letters in lower case
     "drives_fail": true,                             the listing is refused ("boom")
     "drives_delay": 2.0,                             seconds the listing takes to answer (a busy computer, a drive that is slow to say what it holds)
     "slow": 0.05,                                    seconds a read takes per sector (cancel can interrupt it)
     "flaky": [{"from": 100, "to": 120, "times": 1}], the first reads of these sectors from the disc come back with a flipped bit
                                                      ("after": k leaves the first k reads alone; "varies": true flips a different bit each time)
     "bad": [{"from": 500, "to": 502}],               a read of these sectors always fails
     "cache": 512,                                    the drive remembers the last N sectors it read: reading them again gives the same data
                                                      (what a real drive's cache does, and the reason a re-read must first read elsewhere)
     "change_after": 200,                             after this many reads the disc is reported as changed
     "strict_data": true,                             reading a data track as music is a plain failure, not "unsupported"
     "unlisted": ["G"],                               the listing leaves the drive out, but it still answers (a careless helper)
     "burn_fail_at": 120,                             a burn fails (an underrun) when it has written this many sectors, and ruins the disc
     "write_protected": true,                         every burn and erase is refused as a write-protected disc
     "corrupt_burn": true,                            a data disc is written with its first file changed in its last byte (a bad burn that looked fine)
     "no_root": true,                                 a disc with files does not say where they can be read (Windows has not shown them yet)
     "burn_slow": 0.002}                              seconds a burn takes per sector (cancel can interrupt it)
Commands: drives, toc, read, flush, eject, media, burn_audio, burn_data, build_iso, erase, ticks {n} (n progress events), hang (never
answers), crash (says why on stderr, exits), confused (an answer to some other request comes first), nocode (a refusal with no code),
silent (a refusal with a code and no words), close.

The audio is a pure function of (seed, sector) - `audio()` - so a test can work out exactly what a good read must give.
"""

from __future__ import annotations

import array
import base64
import collections
import functools
import json
import math
import os
import queue
import shutil
import sys
import threading
import time
from pathlib import Path

RATE = 44100
FRAMES = 588  # stereo frames in one sector of CD audio
SECTOR_BYTES = FRAMES * 4
_SINE = [int(16000 * math.sin(2 * math.pi * k / RATE)) for k in range(RATE)]  # one second: any whole number of hertz repeats exactly


@functools.lru_cache(maxsize=None)
def sector_audio(seed: int, sector: int) -> bytes:
    """The 2352 bytes (16-bit little-endian stereo) that sector `sector` of a disc with this seed holds: a few sine waves and a little noise."""
    f = (220 + 40 * seed, 660 + 15 * seed, 330 + 25 * seed, 990 + 10 * seed)
    out = array.array("h")
    base = sector * FRAMES
    for n in range(base, base + FRAMES):
        noise = (n * 1103515245 + 12345) % 97 - 48
        out.append((_SINE[(f[0] * n) % RATE] * 2 + _SINE[(f[1] * n) % RATE]) // 3 + noise)
        out.append((_SINE[(f[2] * n) % RATE] * 2 + _SINE[(f[3] * n) % RATE]) // 3 - noise)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def audio(seed: int, first: int, count: int) -> bytes:
    return b"".join(sector_audio(seed, s) for s in range(first, first + count))


# ---------------------------------------------------------------------------------------- building a world (for tests and smokes)


def audio_disc(*, seed: int = 1, tracks: tuple[int, ...] = (60, 80, 70), label: str = "AUDIO CD", data_sectors: int = 0) -> dict:
    """A disc description: audio tracks of these lengths in sectors, and (a CD-Extra) a data track after them if `data_sectors` is set."""
    layout = [{"number": i + 1, "audio": True, "sectors": n} for i, n in enumerate(tracks)]
    if data_sectors:
        layout.append({"number": len(tracks) + 1, "audio": False, "sectors": data_sectors})
    return {"kind": "audio", "label": label, "seed": seed, "tracks": layout}


CD_TYPES = ("CD-R", "CD-RW")
CD_BLANK_SECTORS = 359846  # a blank 80-minute CD-R holds this many sectors (a little under 80 minutes)
SPEEDS = (8, 16, 24, 32, 48)  # what the stand-in writer offers, as multiples of 75 sectors a second


def blank_disc(*, type: str = "CD-R", capacity: int = CD_BLANK_SECTORS, label: str = "") -> dict:
    """A disc that has nothing on it yet. (CD-RW and the other rewritable kinds can be erased back to this.)"""
    return {"kind": "blank", "type": type, "rewritable": type.endswith("RW"), "capacity": capacity, "label": label}


def add_drive(
    root: Path, letter: str, *, name: str = "Stand-in CD/DVD drive", disc: dict | None = None, writer: bool = False, writes: tuple[str, ...] = CD_TYPES
) -> Path:
    folder = Path(root) / letter.upper().rstrip(":")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "drive.json").write_text(json.dumps({"name": name, "writer": writer, "writes": list(writes) if writer else []}), encoding="utf-8")
    if disc is not None:
        (folder / "disc.json").write_text(json.dumps(disc), encoding="utf-8")
    return folder


def set_control(root: Path, **control: object) -> None:
    (Path(root) / "control.json").write_text(json.dumps(control), encoding="utf-8")


CD_EXTRA_GAP = 11400  # the end of the music session, the lead-in of the data session and its pre-gap: sectors that can be read as nothing


def layout(disc: dict) -> tuple[list[dict], int]:
    """The tracks with their start sectors, and the sector where the lead-out begins. A data track after music (an Enhanced CD) starts a
    session of its own, 11400 sectors after the music ends."""
    start = 0
    tracks = []
    for t in disc.get("tracks", []):
        if "start" in t:  # a disc that was burned here says where each track starts (a gap of silence lies between them)
            start = t["start"]
        elif not t["audio"] and tracks and tracks[-1]["audio"]:
            start += CD_EXTRA_GAP
        tracks.append({"number": t["number"], "audio": bool(t["audio"]), "start": start, "sectors": t["sectors"]})
        start += t["sectors"]
    return tracks, start


# ---------------------------------------------------------------------------------------- the stand-in itself


class Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Fake:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cancelled: set[object] = set()
        self.current: object = None
        self.reads = 0
        self.cache: collections.OrderedDict[int, bytes] = collections.OrderedDict()
        self.flaky_used: collections.Counter = collections.Counter()

    def control(self) -> dict:
        try:
            return json.loads((self.root / "control.json").read_text())
        except (OSError, ValueError):
            return {}

    def folder(self, drive: str) -> Path:
        letter = str(drive or "").upper().rstrip(":\\/")
        folder = self.root / letter
        if len(letter) != 1 or not (folder / "drive.json").exists() or letter in self.control().get("unplugged", []):
            raise Refusal("not_found", "That drive is not there any more.")
        return folder

    def disc(self, drive: str) -> dict:
        try:
            return json.loads((self.folder(drive) / "disc.json").read_text())
        except FileNotFoundError:
            raise Refusal("no_media", "There is no disc in the drive.") from None

    def pause(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.current in self.cancelled:
                self.cancelled.discard(self.current)
                raise Refusal("cancelled", "Stopped.")
            time.sleep(0.01)

    def audio_of(self, disc: dict, drive: str, number: int) -> bytes:
        """What sector `number` holds: the pure function of the seed, or (for a disc that was burned here) what was written."""
        if disc.get("pcm"):
            with (self.folder(drive) / disc["pcm"]).open("rb") as stored:
                stored.seek(number * SECTOR_BYTES)
                return stored.read(SECTOR_BYTES).ljust(SECTOR_BYTES, b"\0")
        return sector_audio(int(disc.get("seed", 0)), number)

    def sector(self, disc: dict, tracks: list[dict], number: int, control: dict, drive: str = "") -> bytes:
        track = next((t for t in tracks if t["start"] <= number < t["start"] + t["sectors"]), None)
        if track is None and not (disc.get("pcm") and number < tracks[-1]["start"] + tracks[-1]["sectors"]):  # (the gaps between burned songs can be read: they are silence)
            raise Refusal("read_error", "That sector is outside the disc.")
        if track is not None and not track["audio"]:
            raise Refusal("failed" if control.get("strict_data") else "unsupported", "That is a data track, not audio.")
        size = int(control.get("cache", 0))
        if size and number in self.cache:
            self.cache.move_to_end(number)
            return self.cache[number]  # served from the drive's memory, whatever the disc would give now
        for bad in control.get("bad", []):
            if bad["from"] <= number <= bad["to"]:
                raise Refusal("read_error", f"The drive could not read sector {number}.")
        data = self.audio_of(disc, drive, number)
        for index, flaky in enumerate(control.get("flaky", [])):
            if flaky["from"] <= number <= flaky["to"]:
                self.flaky_used[(index, number)] += 1
                nth = self.flaky_used[(index, number)]  # this is the nth time the sector is read from the disc itself (not from the cache)
                if int(flaky.get("after", 0)) < nth <= int(flaky.get("after", 0)) + int(flaky.get("times", 1)):
                    broken = bytearray(data)
                    broken[(1000 + (nth if flaky.get("varies") else 0)) % len(broken)] ^= 0x01
                    data = bytes(broken)
        if size:
            self.cache[number] = data
            while len(self.cache) > size:
                self.cache.popitem(last=False)
        return data

    def read(self, request: dict, keep: bool) -> bytes:
        disc = self.disc(request.get("drive", ""))
        control = self.control()
        tracks, total = layout(disc)
        if disc.get("kind") != "audio":
            raise Refusal("unsupported", "This is not an audio disc.")
        lba, count = int(request.get("lba", -1)), int(request.get("count", 0))
        if lba < 0 or not 1 <= count <= (16 if keep else 4096):
            raise Refusal("failed", "Bad sector range.")
        self.reads += 1
        if control.get("change_after") and self.reads > int(control["change_after"]):
            raise Refusal("media_changed", "The disc was taken out or changed while it was being read.")
        out = []
        for number in range(lba, lba + count):
            if keep and control.get("slow"):
                self.pause(float(control["slow"]))
            out.append(self.sector(disc, tracks, number, control, request.get("drive", "")))
        return b"".join(out) if keep else b""

    # ------------------------------------------------------------------------------------- writing

    def info(self, drive: str) -> dict:
        return json.loads((self.folder(drive) / "drive.json").read_text())

    def writable(self, request: dict) -> tuple[Path, dict, dict]:
        """The drive's folder, its description and the disc in it, for a command that writes - refused the way the real helper refuses."""
        drive = request.get("drive", "")
        folder, info = self.folder(drive), self.info(drive)
        if not info.get("writer"):
            raise Refusal("unsupported", "This drive cannot write discs.")
        disc = self.disc(drive)
        if self.control().get("write_protected"):
            raise Refusal("write_protected", "The disc is write protected.")
        return folder, info, disc

    def media(self, request: dict) -> dict:
        """What is in the drive, as far as writing to it goes."""
        drive = request.get("drive", "")
        folder, info = self.folder(drive), self.info(drive)
        reply = {"ok": True, "writer": bool(info.get("writer")), "writes": list(info.get("writes", [])),
                 "speeds": [{"speed": 75 * x, "label": f"{x}x"} for x in SPEEDS] if info.get("writer") else []}
        try:
            disc = json.loads((folder / "disc.json").read_text())
        except FileNotFoundError:
            return {**reply, "state": "none", "type": "", "rewritable": False, "blank": False, "capacity_sectors": 0, "free_sectors": 0, "can_audio": False}
        blank = disc.get("kind") == "blank"
        kind = disc.get("type", "CD-ROM")
        capacity = int(disc.get("capacity", 0))
        reply.update(state="blank" if blank else "written", type=kind, rewritable=bool(disc.get("rewritable")), blank=blank, capacity_sectors=capacity,
                     free_sectors=capacity if blank else 0, can_audio=bool(blank and info.get("writer") and kind in CD_TYPES and kind in info.get("writes", [])))
        if disc.get("kind") == "data" and not self.control().get("no_root"):
            reply["root"] = str(folder / "files")  # where the files of the disc can be read (Windows gives the drive letter)
        return reply

    def ruin(self, folder: Path, disc: dict) -> None:
        """A write that stopped half way leaves a disc nobody can use (and a CD-RW can be erased)."""
        (folder / "disc.pcm").unlink(missing_ok=True)
        (folder / "disc.json").write_text(json.dumps({"kind": "unknown", "type": disc.get("type", ""), "rewritable": bool(disc.get("rewritable")),
                                                      "capacity": int(disc.get("capacity", 0)), "label": ""}), encoding="utf-8")

    def burn_audio(self, request: dict, emit) -> dict:
        folder, info, disc = self.writable(request)
        kind = disc.get("type", "")
        if disc.get("kind") != "blank":
            raise Refusal("not_blank", "The disc is not blank.")
        if kind not in CD_TYPES or kind not in info.get("writes", []):
            raise Refusal("unsupported", "Music can only be written to a blank CD-R or CD-RW.")
        tracks = request.get("tracks") or []
        if not 1 <= len(tracks) <= 99:
            raise Refusal("failed", "A music CD holds 1 to 99 songs.")
        lengths = []
        for item in tracks:
            path = Path(str(item.get("path", "")))
            if not path.is_file() or not path.stat().st_size or path.stat().st_size % SECTOR_BYTES:
                raise Refusal("failed", f"{path.name} is not audio the drive can write.")
            lengths.append(max(path.stat().st_size // SECTOR_BYTES, 4 * 75))  # (a track is at least four seconds long)
        need = sum(lengths) + 150 * (len(lengths) - 1)  # two seconds of silence between songs
        capacity = int(disc.get("capacity", 0))
        if need > capacity:
            raise Refusal("no_space", f"The songs need {need} sectors and the disc holds {capacity}.")
        control = self.control()
        written, starts = 0, []
        try:
            with (folder / "disc.pcm").open("wb") as out:
                for index, (item, sectors) in enumerate(zip(tracks, lengths), start=1):
                    if index > 1:
                        out.write(b"\0" * (150 * SECTOR_BYTES))
                        written += 150
                    starts.append(written)
                    data = Path(item["path"]).read_bytes()
                    data += b"\0" * (sectors * SECTOR_BYTES - len(data))
                    for first in range(0, sectors, 64):
                        chunk = data[first * SECTOR_BYTES:(first + 64) * SECTOR_BYTES]
                        count = len(chunk) // SECTOR_BYTES
                        if control.get("burn_slow"):
                            self.pause(float(control["burn_slow"]) * count)
                        if control.get("burn_fail_at") is not None and written + count > int(control["burn_fail_at"]):
                            raise Refusal("underrun", "The drive did not get its data fast enough, and the disc is spoiled.")
                        out.write(chunk)
                        written += count
                        emit({"event": "progress", "id": request.get("id"), "done": written, "total": need, "message": f"Song {index} of {len(tracks)}"})
        except Refusal:
            self.ruin(folder, disc)
            raise
        (folder / "disc.json").write_text(json.dumps({
            "kind": "audio", "label": "", "seed": 0, "pcm": "disc.pcm", "type": kind, "rewritable": bool(disc.get("rewritable")), "capacity": capacity,
            "tracks": [{"number": i + 1, "audio": True, "sectors": n, "start": starts[i]} for i, n in enumerate(lengths)],
        }), encoding="utf-8")
        self.cache.clear()
        if request.get("eject"):
            self.eject(request.get("drive", ""))
        return {"ok": True, "tracks": len(tracks), "sectors": written}

    @staticmethod
    def parts(dest: object) -> list[str]:
        names = [n for n in str(dest or "").replace("\\", "/").split("/") if n]
        if not names or any(n in (".", "..") or ":" in n for n in names):
            raise Refusal("failed", f"{dest!r} is not a place on a disc.")
        return names

    def burn_data(self, request: dict, emit) -> dict:
        folder, info, disc = self.writable(request)
        if disc.get("kind") != "blank":
            raise Refusal("not_blank", "The disc is not blank.")
        files = request.get("files") or []
        if not files:
            raise Refusal("failed", "There is nothing to write.")
        sizes = []
        for item in files:
            path = Path(str(item.get("src", "")))
            if not path.is_file():
                raise Refusal("failed", f"{path.name or 'A file'} is not there.")
            sizes.append(path.stat().st_size)
        places = [self.parts(item.get("dest")) for item in files]  # (every name is looked at before the disc is touched)
        need = sum(-(-n // 2048) for n in sizes) + 32  # whole sectors, and a little for the file system itself
        capacity = int(disc.get("capacity", 0))
        if need > capacity:
            raise Refusal("no_space", f"The files need {need} sectors and the disc holds {capacity}.")
        root = folder / "files"
        shutil.rmtree(root, ignore_errors=True)
        manifest, done = [], 0
        try:
            for item, size, names in zip(files, sizes, places):
                target = root.joinpath(*names)
                target.parent.mkdir(parents=True, exist_ok=True)
                if self.control().get("burn_slow"):
                    self.pause(float(self.control()["burn_slow"]) * max(size // 2048, 1))
                shutil.copyfile(item["src"], target)
                manifest.append({"dest": "/".join(names), "size": size})
                done += -(-size // 2048)
                emit({"event": "progress", "id": request.get("id"), "done": done, "total": need, "message": f"File {len(manifest)} of {len(files)}"})
        except Refusal:
            shutil.rmtree(root, ignore_errors=True)
            self.ruin(folder, disc)
            raise
        if self.control().get("corrupt_burn"):
            first = root.joinpath(*places[0])
            damaged = bytearray(first.read_bytes())
            damaged[-1] ^= 1
            first.write_bytes(bytes(damaged))
        (folder / "disc.json").write_text(json.dumps({
            "kind": "data", "label": str(request.get("label") or ""), "seed": 0, "type": disc.get("type", ""), "rewritable": bool(disc.get("rewritable")),
            "capacity": capacity, "files": manifest, "tracks": [{"number": 1, "audio": False, "sectors": max(need, 1)}],
        }), encoding="utf-8")
        if request.get("eject"):
            self.eject(request.get("drive", ""))
        return {"ok": True, "files": len(files), "sectors": need}

    def build_iso(self, request: dict, emit) -> dict:
        """Needs no drive. The stand-in's image is a header of what is in it, then every file in whole sectors - the size is what matters."""
        files = request.get("files") or []
        out = Path(str(request.get("out", "")))
        if out.suffix.lower() != ".iso" or not out.parent.is_dir():
            raise Refusal("failed", "That is not a place an image can be written.")
        if out.exists():
            raise Refusal("failed", "A file with that name is already there.")
        if not files:
            raise Refusal("failed", "There is nothing to write.")
        blocks, listing = [], []
        for item in files:
            path = Path(str(item.get("src", "")))
            if not path.is_file():
                raise Refusal("failed", f"{path.name or 'A file'} is not there.")
            names = self.parts(item.get("dest"))
            data = path.read_bytes()
            listing.append({"dest": "/".join(names), "size": len(data)})
            blocks.append(data + b"\0" * (-len(data) % 2048))
        header = json.dumps({"label": str(request.get("label") or ""), "files": listing}).encode()
        image = header + b"\0" * (-len(header) % 2048) + b"".join(blocks)
        out.write_bytes(image)
        emit({"event": "progress", "id": request.get("id"), "done": len(image) // 2048, "total": len(image) // 2048, "message": "Image written"})
        return {"ok": True, "sectors": len(image) // 2048, "bytes": len(image)}

    def erase(self, request: dict, emit=None) -> dict:
        folder, info, disc = self.writable(request)
        if disc.get("kind") == "blank":
            return {"ok": True}
        if not disc.get("rewritable"):
            raise Refusal("unsupported", "This disc cannot be erased.")
        if emit:
            emit({"event": "progress", "id": request.get("id"), "done": 0, "total": 0, "message": "Erasing the disc (1 seconds so far)"})
        if self.control().get("burn_slow"):
            self.pause(float(self.control()["burn_slow"]) * 100)
        (folder / "disc.pcm").unlink(missing_ok=True)
        shutil.rmtree(folder / "files", ignore_errors=True)
        (folder / "disc.json").write_text(json.dumps(blank_disc(type=disc.get("type", "CD-RW"), capacity=int(disc.get("capacity", CD_BLANK_SECTORS)))), encoding="utf-8")
        self.cache.clear()
        return {"ok": True}

    def eject(self, drive: str) -> None:
        folder = self.folder(drive)
        (folder / "disc.json").unlink(missing_ok=True)
        (folder / "disc.pcm").unlink(missing_ok=True)
        shutil.rmtree(folder / "files", ignore_errors=True)
        with (folder / "eject.log").open("a", encoding="utf-8") as log:
            log.write("ejected\n")
        self.cache.clear()

    def handle(self, request: dict, emit) -> dict:
        cmd = request.get("cmd")
        if cmd == "hang":
            time.sleep(60)
        if cmd == "crash":
            print("the stand-in crashed on purpose", file=sys.stderr, flush=True)
            os._exit(3)
        noise = self.control().get("noise")
        if noise:
            for line in noise if isinstance(noise, list) else ["stray log line from a driver"]:
                print(line, flush=True)
        if cmd == "confused":
            emit({"id": int(request.get("id") or 0) + 1000, "ok": True, "wrong": True})
            return {"ok": True}
        if cmd == "nocode":
            return {"ok": False, "error": "it went wrong, and the helper gave no code"}
        if cmd == "silent":
            return {"ok": False, "code": "no_media"}
        if cmd == "drives" and self.control().get("drives_delay"):
            self.pause(float(self.control()["drives_delay"]))
        if cmd == "drives" and self.control().get("drives_fail"):
            raise Refusal("failed", "boom")
        if cmd == "drives":
            found = []
            for folder in sorted(p for p in self.root.iterdir() if p.is_dir() and (p / "drive.json").exists()):
                if folder.name in self.control().get("unplugged", []) or folder.name in self.control().get("unlisted", []):  # (unlisted: still answers, as a careless helper might)
                    continue
                info = json.loads((folder / "drive.json").read_text())
                try:
                    disc = json.loads((folder / "disc.json").read_text())
                except FileNotFoundError:
                    disc = {}
                letter = folder.name.lower() if self.control().get("lowercase") else folder.name
                kind = disc.get("kind", "none")
                found.append({"id": f"{letter}:", "name": info.get("name", folder.name), "media": "unknown" if kind == "blank" else kind, "label": disc.get("label", ""),  # (a blank disc has no table of contents to read: to a listing it is one the drive cannot read, as with the real helper)
                              "writer": bool(info.get("writer")), "writes": list(info.get("writes", []))})
            return {"ok": True, "drives": found}
        if cmd == "toc":
            disc = self.disc(request.get("drive", ""))
            if disc.get("kind") == "blank":
                raise Refusal("unsupported", "This disc is blank.")
            tracks, total = layout(disc)
            return {"ok": True, "first": tracks[0]["number"], "last": tracks[-1]["number"], "leadout": total,
                    "tracks": [{"number": t["number"], "audio": t["audio"], "start": t["start"]} for t in tracks]}
        if cmd == "read":
            data = self.read(request, keep=True)
            return {"ok": True, "sectors": int(request["count"]), "data": base64.b64encode(data).decode("ascii")}
        if cmd == "flush":
            self.read(request, keep=False)
            return {"ok": True}
        if cmd == "eject":
            self.eject(request.get("drive", ""))
            return {"ok": True}
        if cmd == "media":
            return self.media(request)
        if cmd == "burn_audio":
            return self.burn_audio(request, emit)
        if cmd == "burn_data":
            return self.burn_data(request, emit)
        if cmd == "build_iso":
            return self.build_iso(request, emit)
        if cmd == "erase":
            return self.erase(request, emit)
        if cmd == "ticks":
            for i in range(int(request.get("n", 3))):
                emit({"event": "progress", "id": request.get("id"), "done": i + 1, "total": int(request.get("n", 3)), "message": f"step {i + 1}"})
            return {"ok": True}
        return {"ok": False, "code": "failed", "error": f"unknown command {cmd!r}"}


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--make":
        root = Path(sys.argv[2])
        add_drive(root, "E", disc=audio_disc(seed=2, tracks=(40, 50), label="SMOKE DISC"))
        add_drive(root, "F")
        return
    fake = Fake(Path(sys.argv[1]))
    work: queue.Queue = queue.Queue()
    out_lock = threading.Lock()

    def emit(message: dict) -> None:
        with out_lock:
            print(json.dumps(message), flush=True)

    def read_input() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                continue
            if request.get("cmd") == "cancel":  # read at once, even while a command is running
                fake.cancelled.add(request.get("target"))
                continue
            work.put(request)
        work.put(None)

    threading.Thread(target=read_input, daemon=True).start()
    emit({"event": "ready", "version": 1})
    while True:
        request = work.get()
        if request is None:
            break
        if request.get("cmd") == "close":
            emit({"id": request.get("id"), "ok": True})
            break
        fake.current = request.get("id")
        try:
            reply = fake.handle(request, emit)
        except Refusal as refusal:
            reply = {"ok": False, "code": refusal.code, "error": refusal.message}
        except Exception as exc:  # the real helper also turns any slip into an error reply
            reply = {"ok": False, "code": "failed", "error": f"{type(exc).__name__}: {exc}"}
        reply["id"] = request.get("id")
        emit(reply)


if __name__ == "__main__":
    main()
