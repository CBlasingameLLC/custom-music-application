"""A stand-in for mtk-disc.exe that speaks the same JSON lines but keeps its drives and discs in plain folders.

    python tests/fake_disc_helper.py <root>          run it (what MTK_DISC_HELPER points at)
    python tests/fake_disc_helper.py --make <root>   make a small world to run it in: drive E: with a 2-track audio disc, drive F: empty

<root>/<letter>/drive.json   {"name": "Stand-in CD/DVD drive"}            one folder per drive (E, F, ...)
<root>/<letter>/disc.json    present while a disc is in the drive:
                             {"kind": "audio" | "data" | "blank", "label": "...", "seed": 1,
                              "tracks": [{"number": 1, "audio": true, "sectors": 300}, ...]}     (laid out one after the other from sector 0)
<root>/control.json          optional, read before every command, to make things go wrong:
    {"unplugged": ["F"],                              the drive is gone
     "noise": true,                                   stray output between replies (or a list of the lines to print)
     "lowercase": true,                               the listing gives drive letters in lower case
     "drives_fail": true,                             the listing is refused ("boom")
     "slow": 0.05,                                    seconds a read takes per sector (cancel can interrupt it)
     "flaky": [{"from": 100, "to": 120, "times": 1}], the first reads of these sectors from the disc come back with a flipped bit
                                                      ("after": k leaves the first k reads alone; "varies": true flips a different bit each time)
     "bad": [{"from": 500, "to": 502}],               a read of these sectors always fails
     "cache": 512,                                    the drive remembers the last N sectors it read: reading them again gives the same data
                                                      (what a real drive's cache does, and the reason a re-read must first read elsewhere)
     "change_after": 200,                             after this many reads the disc is reported as changed
     "strict_data": true}                             reading a data track as music is a plain failure, not "unsupported"
Commands: drives, toc, read, flush, eject, ticks {n} (n progress events), hang (never answers), crash (says why on stderr, exits),
confused (an answer to some other request comes first), nocode (a refusal with no code), silent (a refusal with a code and no words), close.

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


def add_drive(root: Path, letter: str, *, name: str = "Stand-in CD/DVD drive", disc: dict | None = None) -> Path:
    folder = Path(root) / letter.upper().rstrip(":")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "drive.json").write_text(json.dumps({"name": name}), encoding="utf-8")
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
        if not t["audio"] and tracks and tracks[-1]["audio"]:
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

    def sector(self, disc: dict, tracks: list[dict], number: int, control: dict) -> bytes:
        track = next((t for t in tracks if t["start"] <= number < t["start"] + t["sectors"]), None)
        if track is None:
            raise Refusal("read_error", "That sector is outside the disc.")
        if not track["audio"]:
            raise Refusal("failed" if control.get("strict_data") else "unsupported", "That is a data track, not audio.")
        size = int(control.get("cache", 0))
        if size and number in self.cache:
            self.cache.move_to_end(number)
            return self.cache[number]  # served from the drive's memory, whatever the disc would give now
        for bad in control.get("bad", []):
            if bad["from"] <= number <= bad["to"]:
                raise Refusal("read_error", f"The drive could not read sector {number}.")
        data = sector_audio(int(disc.get("seed", 0)), number)
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
            out.append(self.sector(disc, tracks, number, control))
        return b"".join(out) if keep else b""

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
                found.append({"id": f"{letter}:", "name": info.get("name", folder.name), "media": disc.get("kind", "none"), "label": disc.get("label", "")})
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
            folder = self.folder(request.get("drive", ""))
            disc = folder / "disc.json"
            if disc.exists():
                disc.unlink()
            with (folder / "eject.log").open("a", encoding="utf-8") as log:
                log.write("ejected\n")
            self.cache.clear()
            return {"ok": True}
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
