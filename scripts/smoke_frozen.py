"""Smoke-test the frozen backend the way a freshly installed app starts it.

    python scripts/smoke_frozen.py dist/mtk-backend.exe tests/fixtures

Fresh user profile, an unrelated working directory, no config, no database.
Launches `<exe> dashboard`, then checks that the bundled web UI is served and
that the token gate, library scan, cover art (Pillow), Range streaming (what
the in-app player uses), the library tools (organize with undo, duplicates with
the review folder and the Recycle Bin), device sync to a folder, the Spotify
import (which needs pandas inside the bundle), listening statistics, mixes and
song radio, the release radar, lyrics from a stand-in for lrclib.net, the
scrobbler status and diagnostics, internet radio and podcasts (against stand-ins for a
station, the Radio Browser and Apple's directories and a podcast host), settings and
backup all work from the frozen binary, and that nothing leaks into the working directory.

Standard library only, so it runs on any CI runner without installing anything.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "smoke-token"
WINDOWS = sys.platform == "win32"
failures: list[str] = []

# CI consoles on Windows default to a legacy code page; the checks print arrows and quotes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def check(ok: bool, message: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + message, flush=True)
    if not ok:
        failures.append(message)


def tiny_png(size: int = 24) -> bytes:
    """A valid PNG (solid colour) without needing any imaging library here."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    row = b"\x00" + bytes([200, 60, 60]) * size
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def request(self, path: str, method: str = "GET", body=None, headers=None, token: bool = True):
        sent = {"X-MTK-Token": TOKEN} if token else {}
        sent.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=sent)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
        except urllib.error.HTTPError as error:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()

    def json(self, path: str, method: str = "GET", body=None):
        status, _, payload = self.request(path, method, body)
        return status, json.loads(payload or b"null")


def wait_job(client: Client, job_id: str, tries: int = 300) -> dict:
    job: dict = {"status": "unknown"}
    for _ in range(tries):
        time.sleep(0.2)
        _, job = client.json(f"/api/jobs/{job_id}")
        if job["status"] in ("done", "error", "cancelled"):
            break
    return job


def stop(process: subprocess.Popen) -> None:
    if WINDOWS:
        # The onefile bootloader's child must die with it; /T walks the tree.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def library_tools(client: Client, music: Path, count: int) -> None:
    """Organize (preview, apply, undo) and the duplicate finder (search, review folder, Recycle Bin)."""
    _, info = client.json("/api/tools/organize/info")
    root, scheme = info["roots"][0]["path"], info["scheme"]
    _, started = client.json("/api/tools/organize/preview", "POST", {"root": root, "scheme": scheme})
    job = wait_job(client, started["job"]["id"])
    check(job["status"] == "done" and job["result"]["moves"] > 0, f"organize preview plans moves ({job.get('result') or job.get('error')})")
    status, started = client.json("/api/tools/organize/apply", "POST", {"root": root, "scheme": scheme, "remember": False})
    job = wait_job(client, started["job"]["id"])
    result = job.get("result") or {}
    check(job["status"] == "done" and result.get("moved", 0) > 0 and not result.get("errors"),
          f"organize moved files ({result.get('moved')} moved, {result.get('errors') or job.get('error')})")
    check(not list(music.glob("*.mp3")), "no song is left loose in the library folder")
    _, started = client.json("/api/tools/organize/undo", "POST", {"batch_id": result["batch_id"]})
    job = wait_job(client, started["job"]["id"])
    check(job["status"] == "done" and (job.get("result") or {}).get("moved") == result["moved"],
          f"organize undo puts every file back ({job.get('result') or job.get('error')})")
    check(len(list(music.glob("*.mp3"))) == count, "the original files are back where they were")

    # a second copy of a song, found by its content, parked in the review folder, then sent to the Recycle Bin
    twin_dir = music / "copies"
    twin_dir.mkdir()
    shutil.copy(next(music.glob("tone_12s.mp3")), twin_dir / "tone_12s copy.mp3")
    _, started = client.json("/api/library/scan", "POST")
    wait_job(client, started["job"]["id"])
    _, started = client.json("/api/tools/duplicates/scan", "POST", {"exact": True})
    scan = wait_job(client, started["job"]["id"])
    check(scan["status"] == "done" and scan["result"]["groups"] >= 1, f"duplicate search finds the copy ({scan.get('result') or scan.get('error')})")
    _, page = client.json(f"/api/tools/duplicates/groups/{started['job']['id']}")
    group = next(g for g in page["items"] if any("copy" in m["path"] for m in g["members"]))
    check(group["identical"], "the copy is recognised as byte for byte identical")
    keeper = group["keeper"]
    choice = {"key": group["key"], "keeper": keeper, "remove": [m["id"] for m in group["members"] if m["id"] != keeper]}
    _, moved = client.json("/api/tools/duplicates/quarantine", "POST", {"job_id": started["job"]["id"], "choices": [choice]})
    job = wait_job(client, moved["job"]["id"])
    check(job["status"] == "done" and job["result"]["moved"] == len(choice["remove"]), f"copy moved to the review folder ({job.get('result') or job.get('error')})")
    check((music / "_duplicates_review").is_dir(), "the review folder exists inside the library folder")
    _, review = client.json("/api/tools/duplicates/review")
    check(review["count"] == len(choice["remove"]), f"review folder lists the copy ({review.get('count')})")
    status, deleted = client.json("/api/tools/duplicates/delete", "POST", {"everything": True, "confirm": "DELETE"})
    job = wait_job(client, deleted["job"]["id"])
    result = job.get("result") or {}
    check(job["status"] == "done" and result.get("moved") == len(choice["remove"]) and not result.get("errors"),
          f"copy sent to the Recycle Bin ({result.get('errors') or job.get('error') or 'ok'})")
    check(not (music / "_duplicates_review").exists(), "the emptied review folder is gone")


def devices_and_accounts(client: Client, scratch: Path, count: int) -> None:
    """A folder standing in for a card, the account helpers, and the Spotify import (pandas has to work in here)."""
    card = scratch / "card"
    card.mkdir()
    status, device = client.json("/api/devices", "POST", {"path": str(card)})
    check(status == 201 and device["connected"] and device["default_scheme"], f"a folder becomes a device (HTTP {status})")
    status, volumes = client.json("/api/devices/volumes")
    check(status == 200 and isinstance(volumes["items"], list), "drives can be listed")
    _, started = client.json(f"/api/devices/{device['id']}/preview", "POST", {"sources": [{"kind": "all"}]})
    preview = wait_job(client, started["job"]["id"])
    result = preview.get("result") or {}
    check(preview["status"] == "done" and result.get("to_copy", 0) >= 1 and result["to_copy"] + result["skipped"] == count and result["enough_space"],
          f"sync preview lists what would be copied ({result or preview.get('error')})")
    check(not list(card.rglob("*.mp3")), "a preview writes nothing to the device")
    status, started = client.json(f"/api/devices/{device['id']}/sync", "POST", {"job_id": preview["id"]})
    done = wait_job(client, started["job"]["id"])
    copied = (done.get("result") or {}).get("copied", 0)
    check(status == 200 and done["status"] == "done" and copied == result["to_copy"] and not done["result"]["errors"],
          f"songs are copied to the device ({done.get('result') or done.get('error')})")
    check(len(list(card.rglob("*.mp3"))) == copied, "the copied files are on the device")

    status, scrobbler = client.json("/api/scrobbler")
    check(status == 200 and scrobbler["state"] == "off" and not scrobbler["has_token"], f"the scrobbler is idle without a token ({scrobbler.get('state')})")
    status, tested = client.json("/api/settings/test/musicbrainz", "POST", {})
    check(status == 200 and tested["ok"] is False and "contact" in tested["message"], "Test connection explains what is missing")
    status, report = client.json("/api/system/diagnostics")
    check(status == 200 and report["app"]["installed"] is True and report["database"]["songs"] == count and report["database"]["schema_version"] >= 6,
          f"the diagnostics report describes the installed app ({report.get('app')})")

    export = scratch / "spotify-export.zip"
    rows = [
        {"ts": "2024-01-15T10:00:00Z", "ms_played": 210000, "master_metadata_track_name": "Song One", "master_metadata_album_artist_name": "Artist A",
         "master_metadata_album_album_name": "Album A", "spotify_track_uri": "spotify:track:aaa111"},
        {"ts": "2024-01-17T09:00:00Z", "ms_played": 900000, "master_metadata_track_name": None, "master_metadata_album_artist_name": None,
         "master_metadata_album_album_name": None, "spotify_track_uri": None, "episode_name": "A podcast"},
    ]
    with zipfile.ZipFile(export, "w") as archive:
        archive.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2024_0.json", json.dumps(rows))
    _, started = client.json("/api/history/import/preview", "POST", {"path": str(export)})
    read = wait_job(client, started["job"]["id"])
    check(read["status"] == "done" and read["result"]["music_rows"] == 1 and read["result"]["podcast_rows_skipped"] == 1,
          f"a Spotify export is read ({read.get('result') or read.get('error')})")
    status, started = client.json("/api/history/import/apply", "POST", {"job_id": read["id"]})
    added = wait_job(client, started["job"]["id"])
    check(status == 200 and added["status"] == "done" and added["result"]["inserted"] == 1, f"the plays are added ({added.get('result') or added.get('error')})")
    status, imports = client.json("/api/history/imports")
    check(status == 200 and len(imports["items"]) == 1 and imports["items"][0]["plays"] == 1, "the import is listed so it can be undone")


def phones(client: Client, scratch: Path, count: int) -> None:
    """A phone without a drive letter, reached through a stand-in for the helper that the frozen backend starts itself."""
    status, listing = client.json("/api/devices/mtp?fresh=true")
    check(status == 200 and listing["available"] and not listing["error"] and [d["serial"] for d in listing["devices"]] == ["SER1"],
          f"the backend starts the helper and lists the phone ({listing})")
    status, device = client.json("/api/devices/mtp", "POST", {"serial": "SER1", "storage": "s1"})
    check(status == 201 and device["kind"] == "mtp" and device["connected"] and device["free"], f"the phone becomes a device ({device})")
    _, started = client.json(f"/api/devices/{device['id']}/preview", "POST", {"sources": [{"kind": "all"}]})
    preview = wait_job(client, started["job"]["id"])
    result = preview.get("result") or {}
    check(preview["status"] == "done" and result.get("to_copy", 0) >= 1 and result["to_copy"] + result["skipped"] == count,
          f"the phone's sync preview lists what would be copied ({result or preview.get('error')})")
    music = scratch / "phones" / "SER1" / "s1" / "Music"
    check(not list(music.rglob("*.mp3")), "a preview writes nothing to the phone")
    status, started = client.json(f"/api/devices/{device['id']}/sync", "POST", {"job_id": preview["id"]})
    done = wait_job(client, started["job"]["id"])
    copied = (done.get("result") or {}).get("copied", 0)
    check(status == 200 and done["status"] == "done" and copied == result["to_copy"] and not done["result"]["errors"],
          f"songs are copied to the phone ({done.get('result') or done.get('error')})")
    check(len(list(music.rglob("*.mp3"))) == copied, "the copied files are on the phone")
    status, described = client.json(f"/api/devices/{device['id']}")
    check(status == 200 and described["synced"] == copied, "the phone remembers what was copied")


def discs(client: Client, scratch: Path, music: Path) -> None:
    """A CD in a stand-in drive: found, looked up on a stand-in MusicBrainz, and ripped to FLAC and to MP3 inside the frozen backend. That proves what only
    the frozen exe can: that the audio library (libsndfile with its FLAC and MP3 encoders), the tagger, the image library and the client for the disc
    helper were all bundled, and that nothing the rip needs is looked for on this machine's Python."""
    status, listing = client.json("/api/discs/drives?fresh=true")
    drives = {d["id"]: d["media"] for d in listing.get("drives", [])}
    check(status == 200 and listing["available"] and not listing["error"] and drives == {"E:": "audio", "F:": "none"},
          f"the backend starts the disc helper and lists the drives ({listing})")
    status, disc = client.json("/api/discs/drives/E:/disc")
    check(status == 200 and len(disc["tracks"]) == 2 and disc["disc_id"], f"it reads the disc's table of contents ({disc})")
    status, _ = client.json("/api/settings", "PUT", {"musicbrainz": {"contact": "smoke@example.com"}})
    check(status == 200, "the MusicBrainz contact is saved")
    status, started = client.json("/api/discs/lookup", "POST", {"drive": "E:"})
    job = wait_job(client, started["job"]["id"])
    found = (job.get("result") or {}).get("candidates") or []
    check(job["status"] == "done" and [c["title"] for c in found] == ["Smoke Album"], f"the album is found on MusicBrainz ({job.get('result') or job.get('error')})")
    if not found:
        return
    album = found[0]

    def rip(kind: str, quality: str, name: str) -> dict:
        asked = {
            "drive": "E:", "root": str(music), "scheme": "{album_artist}/{album}/{track:02d} - {title}.{ext}", "format": kind, "quality": quality,
            "accuracy": "verified", "album": name, "album_artist": album["artist"], "year": album["year"], "date": album["date"], "genre": "Smoke",
            "disc_number": 1, "disc_total": 1, "release_id": album["release_id"], "release_group_id": album["release_group_id"],
            "artist_ids": album["artist_ids"], "barcode": album["barcode"], "label": album["label"],
            "tracks": [{"number": t["number"], "title": t["title"], "artist": t["artist"], "recording_id": t["recording_id"], "rip": True} for t in album["tracks"]],
            "cover": True, "eject": False,
        }
        status, plan = client.json("/api/discs/rip/preview", "POST", asked)
        check(status == 200 and plan["ok"] and plan["counts"]["rip"] == 2, f"the {kind} rip is previewed ({plan})")
        status, begun = client.json("/api/discs/rip/start", "POST", asked)
        finished = wait_job(client, begun["job"]["id"], tries=600)
        result = finished.get("result") or {}
        check(status == 200 and finished["status"] == "done" and result.get("ripped") == 2 and not result.get("failed"),
              f"the {kind} rip finishes ({result or finished.get('error')})")
        return begun

    rip("flac", "standard", "Smoke Album")
    folder = music / "Smoke Artist" / "Smoke Album"
    flacs = sorted(folder.glob("*.flac"))
    check([f.name for f in flacs] == ["01 - Smoke One.flac", "02 - Smoke Two.flac"] and all(f.read_bytes()[:4] == b"fLaC" for f in flacs), f"the songs are FLAC files ({[f.name for f in flacs]})")
    cover = folder / "cover.jpg"
    check(cover.exists() and cover.read_bytes()[:2] == b"\xff\xd8", "the cover was fetched, re-drawn as a JPEG and put beside the songs")
    rip("mp3", "128", "Smoke Album (MP3)")
    mp3s = sorted((music / "Smoke Artist" / "Smoke Album (MP3)").glob("*.mp3"))
    check(len(mp3s) == 2 and all(f.read_bytes()[:3] == b"ID3" for f in mp3s), f"the songs are MP3 files with tags ({[f.name for f in mp3s]})")

    status, library = client.json("/api/tracks?limit=200")
    ripped = {(t["title"], t["format"]): t for t in library["items"] if t["album"].startswith("Smoke Album")}
    check(set(ripped) == {("Smoke One", "flac"), ("Smoke Two", "flac"), ("Smoke One", "mp3"), ("Smoke Two", "mp3")}, f"the songs joined the library, tagged ({sorted(ripped)})")
    flac = ripped.get(("Smoke One", "flac"))
    if flac:
        check(abs(flac["duration"] - 40 / 75) < 0.1 and flac["artist"] == "Smoke Artist", f"a song has the length of the track on the disc ({flac['duration']})")
        status, headers, body = client.request(f"/api/art/track/{flac['id']}?size=64")
        check(status == 200 and headers.get("content-type", "").startswith("image/") and len(body) > 50, f"the cover is inside the song ({status})")
    mp3 = ripped.get(("Smoke One", "mp3"))
    if mp3:
        _, info = client.json(f"/api/tracks/{mp3['id']}/info")
        check(abs((info.get("bitrate") or 0) / 1000 - 128) <= 2, f"the MP3 has the bit rate asked for ({info.get('bitrate')})")
    status, history = client.json("/api/discs/rips")
    check(status == 200 and [(r["status"], r["songs"]) for r in history["rips"]] == [("done", 2), ("done", 2)], f"both rips are in the history ({history})")
    left = [p.name for p in music.rglob("*.part")] + [p.name for p in (scratch / "profile" / ".musictoolkit" / "cache" / "discs").glob("*")]
    check(not left, f"no half-written song or temporary copy is left ({left})")


LRCLIB_ANSWER = {"id": 7, "instrumental": False, "plainLyrics": "Sparks\nand wire", "syncedLyrics": "[00:01.00]Sparks\n[00:04.00]and wire\n"}


def stand_in_for_lrclib() -> tuple[ThreadingHTTPServer, str]:
    """A local server that answers every lyrics request the way lrclib.net does for a song it has."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (the name http.server looks for)
            payload = json.dumps(LRCLIB_ANSWER).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def personal_layer(client: Client, tracks: list[dict]) -> None:
    """Statistics, mixes, radio, the release radar and online lyrics, on top of what the earlier checks left in the library."""
    status, years = client.json("/api/stats/years?tz=0")
    check(status == 200 and {"year": 2024, "plays": 1} in years["items"], f"statistics know the imported year ({years})")
    status, overview = client.json("/api/stats/overview?range=year:2024&tz=0")
    check(status == 200 and overview["totals"]["plays"] == 1 and overview["top_artists"][0]["name"] == "Artist A",
          f"a year's statistics ({overview.get('totals')})")
    check(client.request("/api/stats/overview?range=nonsense")[0] == 422, "a nonsense period is refused")
    status, mixes = client.json("/api/mixes?tz=0")
    check(status == 200 and isinstance(mixes["items"], list), f"the mixes that have enough songs today ({len(mixes.get('items', []))})")
    seed = tracks[0]
    status, radio = client.json(f"/api/radio/track/{seed['id']}")
    check(status == 200 and radio["tracks"] and radio["tracks"][0]["id"] == seed["id"], f"a radio starts with its song ({status})")

    status, releases = client.json("/api/releases")
    check(status == 200 and releases["items"] == [] and releases["enabled"] is False, f"the release radar starts empty and off ({releases})")
    status, started = client.json("/api/releases/refresh", "POST", {})
    job = wait_job(client, started["job"]["id"]) if status == 200 else {}
    check(job.get("status") == "error" and "nowhere to look" in (job.get("error") or ""),
          f"with nowhere to look the radar says what to add ({job.get('status')}: {job.get('error')})")

    named = next((t for t in tracks if t.get("title") and t.get("artist")), None)
    if named is None:
        check(False, "a fixture song with an artist and a title to look lyrics up for")
        return
    status, before = client.json(f"/api/tracks/{named['id']}/lyrics")
    check(status == 200 and before["source"] is None and before["online"] == "off", f"lookups are off until asked for ({before})")
    status, found = client.json(f"/api/tracks/{named['id']}/lyrics/lookup", "POST", {})
    check(status == 200 and found["source"] == "lrclib" and found["synced"] and found["synced"][0]["text"] == "Sparks",
          f"lyrics from the stand-in for lrclib.net ({found.get('online')})")
    status, again = client.json(f"/api/tracks/{named['id']}/lyrics")
    check(status == 200 and again["source"] == "lrclib", "and they are kept in the app's database")


def mp3_frames(data: bytes) -> bytes:
    """An MP3's audio without its ID3v2 tag, so it can be repeated end to end like a live stream."""
    if data[:3] == b"ID3":
        return data[10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]) :]
    return data


def stand_in_for_the_internet(tone: bytes) -> tuple[list[ThreadingHTTPServer], dict[str, str]]:
    """Local servers standing in for a radio station, the Radio Browser directory, Apple's podcast directory and a podcast host. (The backend is
    told to connect to addresses on this computer, which it refuses when it really runs.)"""
    frames = mp3_frames(tone)
    servers: list[ThreadingHTTPServer] = []
    urls: dict[str, str] = {}

    def serve(name: str, handler) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (the name http.server looks for)
                try:
                    handler(self)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # the listener went away

            def log_message(self, *_args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        urls[name] = f"http://127.0.0.1:{server.server_address[1]}"

    def send_json(h: BaseHTTPRequestHandler, payload) -> None:
        body = json.dumps(payload).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def station(h: BaseHTTPRequestHandler) -> None:
        wants = h.headers.get("Icy-MetaData") == "1"
        h.send_response(200)
        h.send_header("Content-Type", "audio/mpeg")
        h.send_header("icy-name", "Smoke FM")
        h.send_header("icy-genre", "jazz, smooth")
        h.send_header("icy-br", "48")
        if wants:
            h.send_header("icy-metaint", "4096")
        h.end_headers()
        position = 0
        while True:  # a live station does not end
            block = bytearray()
            while len(block) < 4096:
                piece = frames[position : position + 4096 - len(block)]
                block += piece
                position = (position + len(piece)) % len(frames)
            h.wfile.write(bytes(block))
            if wants:
                meta = b"StreamTitle='Smoke Artist - Smoke Title';"
                padded = meta + b"\0" * (-len(meta) % 16)
                h.wfile.write(bytes([len(padded) // 16]) + padded)
            h.wfile.flush()
            time.sleep(0.02)

    serve("station", station)
    serve(
        "radio_browser",
        lambda h: send_json(
            h,
            {
                "/stations/search": [{"stationuuid": "aaaaaaaa-1111-2222-3333-444444444444", "name": "Directory Smoke FM", "url_resolved": urls["station"] + "/live",
                                      "country": "Norway", "countrycode": "NO", "codec": "MP3", "bitrate": 48, "tags": "jazz", "votes": 1}],
                "/tags": [{"name": "jazz", "stationcount": 1}],
                "/countries": [{"name": "Norway", "iso_3166_1": "NO", "stationcount": 1}],
            }.get(h.path.split("?")[0], {}),
        ),
    )

    def host(h: BaseHTTPRequestHandler) -> None:
        path = h.path.split("?")[0]
        if path == "/feed.xml":
            items = "".join(
                f"<item><title>Smoke episode {n}</title><guid>smoke-{n}</guid><pubDate>Tue, 0{n} Jun 2025 10:00:00 +0000</pubDate><itunes:duration>12</itunes:duration>"
                f'<enclosure url="{urls["host"]}/ep.mp3" type="audio/mpeg" length="{len(tone)}"/></item>'
                for n in (1, 2)
            )
            body = (
                '<?xml version="1.0"?><rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Smoke Cast</title>'
                f'<itunes:author>Jo Host</itunes:author><itunes:image href="{urls["host"]}/art.png"/>{items}</channel></rss>'
            ).encode()
            h.send_response(200)
            h.send_header("Content-Type", "application/rss+xml")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        elif path == "/ep.mp3":
            asked = (h.headers.get("Range") or "").removeprefix("bytes=").split("-")
            if len(asked) == 2 and asked[0].isdigit():
                start = int(asked[0])
                end = min(int(asked[1]) if asked[1].isdigit() else len(tone) - 1, len(tone) - 1)
                h.send_response(206)
                h.send_header("Content-Range", f"bytes {start}-{end}/{len(tone)}")
                h.send_header("Content-Length", str(end - start + 1))
                h.send_header("Content-Type", "audio/mpeg")
                h.end_headers()
                h.wfile.write(tone[start : end + 1])
            else:
                h.send_response(200)
                h.send_header("Content-Length", str(len(tone)))
                h.send_header("Content-Type", "audio/mpeg")
                h.end_headers()
                h.wfile.write(tone)
        elif path == "/art.png":
            body = tiny_png(64)
            h.send_response(200)
            h.send_header("Content-Type", "image/png")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        else:
            h.send_response(404)
            h.send_header("Content-Length", "0")
            h.end_headers()

    serve("host", host)
    serve(
        "apple",
        lambda h: send_json(
            h, {"resultCount": 1, "results": [{"collectionId": 7, "collectionName": "Smoke Cast", "artistName": "Jo Host", "feedUrl": urls["host"] + "/feed.xml", "trackCount": 2}]}
        ),
    )
    return servers, urls


def radio_and_podcasts(client: Client, urls: dict[str, str], tone: bytes) -> None:
    """Radio and podcasts from the frozen binary: the directories, a station kept and listened to (its title taken out of the audio), a podcast
    followed, its episodes heard through the app with Range, pictures re-drawn, and the check for new episodes."""
    frames = mp3_frames(tone)
    plays_before = client.json("/api/history/top-artists?limit=5")[1]["total_plays"]
    status, found = client.json("/api/radio/directory?q=smoke")
    check(status == 200 and [s["name"] for s in found["items"]] == ["Directory Smoke FM"], f"the radio directory is searched ({found})")
    status, tags = client.json("/api/radio/directory/tags")
    check(status == 200 and tags["items"][0]["name"] == "jazz", "its kinds of music are listed")
    status, station = client.json("/api/radio/stations", "POST", {"url": urls["station"] + "/live"})
    check(status == 201 and station["name"] == "Smoke FM" and station["favorite"] and station["bitrate"] == 48, f"a station is kept from its address, trying it first ({station})")
    request = urllib.request.Request(client.base + f"/api/radio/stations/{station['id']}/stream", headers={"X-MTK-Token": TOKEN})
    with urllib.request.urlopen(request, timeout=30) as response:
        audio = response.read(12000)
        status_now, now = client.json(f"/api/radio/stations/{station['id']}/now")
        check(response.status == 200 and response.headers.get("Content-Type", "").startswith("audio/mpeg"), "the station plays through the app")
    check(audio == frames[:12000], "its title is taken out of the audio (what arrives is only sound)")
    check(status_now == 200 and now["title"] == "Smoke Artist - Smoke Title" and now["artist"] == "Smoke Artist", f"and kept for the player bar ({now})")
    status, played = client.json(f"/api/radio/stations/{station['id']}/played", "POST", {})
    check(status == 200 and client.json("/api/radio/stations?scope=recent")[1]["counts"]["recent"] == 1, "a station played is remembered")

    status, followed = client.json("/api/podcasts", "POST", {"feed_url": urls["host"] + "/feed.xml"})
    check(status == 201 and followed["title"] == "Smoke Cast" and followed["episodes"] == 2 and followed["new"] == 2, f"a podcast is followed from its feed ({followed})")
    status, shown = client.json(f"/api/podcasts/{followed['id']}")
    episode = shown["items"][0]
    check(status == 200 and [e["title"] for e in shown["items"]] == ["Smoke episode 2", "Smoke episode 1"] and episode["duration"] == 12, "its episodes are listed, newest first")
    status, headers, body = client.request(f"/api/podcasts/episodes/{episode['id']}/stream", headers={"Range": "bytes=100-199"})
    check(status == 206 and body == tone[100:200] and headers.get("content-range", "").startswith("bytes 100-199/"), f"an episode is heard through the app, and can be sought ({status})")
    status, headers, body = client.request(f"/api/podcasts/{followed['id']}/art")
    check(status == 200 and headers.get("content-type") == "image/jpeg" and len(body) > 50, f"the show's picture is re-drawn small ({status} {headers.get('content-type')})")
    status, progress = client.json(f"/api/podcasts/episodes/{episode['id']}/progress", "POST", {"position": 5, "duration": 12})
    check(status == 200 and progress["position"] == 5 and not progress["played"] and progress["new"], f"where listening stopped is kept ({progress})")
    status, finished = client.json(f"/api/podcasts/episodes/{episode['id']}/progress", "POST", {"position": 11.9, "duration": 12})
    check(status == 200 and finished["played"] and finished["position"] == 0 and not finished["new"], "and near the end the episode counts as heard")
    status, search = client.json("/api/podcasts/search?q=smoke&country=US")
    check(status == 200 and search["items"][0]["followed_id"] == followed["id"], "Apple's directory is searched, and says which shows are followed")
    status, started = client.json("/api/podcasts/refresh", "POST")
    job = wait_job(client, started["job"]["id"]) if status == 200 else {}
    check(job.get("status") == "done" and job["result"]["checked"] == 1 and job["result"]["errors"] == 0, f"new episodes are looked for ({job.get('result') or job.get('error')})")
    status, plays = client.json("/api/history/top-artists?limit=5")
    check(status == 200 and plays["total_plays"] == plays_before, f"listening to radio and podcasts adds nothing to the music history ({plays_before} plays before, {plays.get('total_plays')} after)")


def run(exe: Path, fixtures: Path) -> None:
    scratch = Path(tempfile.mkdtemp(prefix="mtk-smoke-"))
    profile, elsewhere, music = scratch / "profile", scratch / "elsewhere", scratch / "music"
    for folder in (profile, elsewhere, music):
        folder.mkdir()
    mp3s = sorted(fixtures.glob("*.mp3"))
    if not mp3s:
        sys.exit(f"no .mp3 fixtures in {fixtures}")
    for mp3 in mp3s:
        shutil.copy(mp3, music / mp3.name)
    (music / "cover.png").write_bytes(tiny_png())

    port = free_port()
    log_path = scratch / "backend.log"
    lrclib, lrclib_url = stand_in_for_lrclib()
    tone = (fixtures / "tone_12s.mp3").read_bytes() if (fixtures / "tone_12s.mp3").exists() else mp3s[0].read_bytes()
    internet, internet_urls = stand_in_for_the_internet(tone)
    env = {
        **os.environ, "HOME": str(profile), "USERPROFILE": str(profile), "MTK_TOKEN": TOKEN, "MTK_LRCLIB_URL": lrclib_url,
        "MTK_ALLOW_LOCAL_URLS": "1", "MTK_RADIO_BROWSER_URL": internet_urls["radio_browser"], "MTK_ITUNES_URL": internet_urls["apple"],
    }
    # A stand-in for mtk-disc.exe that keeps its drives and discs in folders (the real one needs a drive with a disc in it), and for MusicBrainz and the
    # Cover Art Archive: one release, with a picture. Both are plain Python and standard library, kept with the tests.
    fake_disc = fixtures.resolve().parent / "fake_disc_helper.py"
    disc_servers = contextlib.ExitStack()
    if fake_disc.exists():
        sys.path.insert(0, str(fixtures.resolve().parent.parent))
        from tests.fake_musicbrainz import coverart_script, ident, musicbrainz_script, release_xml  # noqa: PLC0415
        from tests.fake_streams import serve_script  # noqa: PLC0415

        subprocess.run([sys.executable, str(fake_disc), "--make", str(scratch / "drives")], check=True)
        release = release_xml(key="smoke", title="Smoke Album", artist="Smoke Artist", tracks=(("Smoke One", 533), ("Smoke Two", 667)))
        mb = disc_servers.enter_context(serve_script(musicbrainz_script(fuzzy=[release])))
        caa = disc_servers.enter_context(serve_script(coverart_script({f"release/{ident('smoke')}": tiny_png(300)})))
        env.update(MTK_DISC_HELPER=json.dumps([sys.executable, str(fake_disc), str(scratch / "drives")]), MTK_MUSICBRAINZ_URL=mb.url, MTK_COVERART_URL=caa.url)
    # A stand-in for mtk-mtp.exe that keeps its "phones" in folders: the real one needs a phone plugged in.
    fake_helper = fixtures.resolve().parent / "fake_mtp_helper.py"
    if fake_helper.exists():
        (scratch / "phones" / "SER1").mkdir(parents=True)
        (scratch / "phones" / "SER1" / "device.json").write_text(json.dumps({
            "name": "Smoke phone", "manufacturer": "Test", "model": "T1",
            "storages": [{"id": "s1", "name": "Internal storage", "capacity": 50_000_000}],
        }))
        env["MTK_MTP_HELPER"] = json.dumps([sys.executable, str(fake_helper), str(scratch / "phones")])
    with open(log_path, "w") as log:
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
        process = subprocess.Popen(
            [str(exe), "dashboard", "--port", str(port)],
            cwd=elsewhere, env=env, stdout=log, stderr=subprocess.STDOUT, **kwargs,
        )
    client = Client(f"http://127.0.0.1:{port}")
    try:
        started = time.time()
        for _ in range(240):  # onefile self-extraction plus antivirus scanning can take a while
            if process.poll() is not None:
                break
            try:
                urllib.request.urlopen(client.base + "/", timeout=2)
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(0.5)
        check(process.poll() is None, f"backend is running ({time.time() - started:.1f}s to answer)")
        if process.poll() is not None:
            return

        status, headers, body = client.request(f"/?token={TOKEN}", token=False)
        check(status == 200 and b"Music Toolkit" in body and "mtk_session" in headers.get("set-cookie", ""),
              f"GET /?token=... serves the UI and sets the session cookie (HTTP {status})")
        check(client.request("/", token=False)[0] == 401, "GET / without the token is refused")
        check(client.request("/api/about", token=False)[0] == 401, "GET /api/about without the token is refused")
        for asset in ("/static/css/app.css", "/static/js/main.js", "/static/js/views/settings.js", "/static/vendor/preact.js"):
            status, _, body = client.request(asset, token=False)
            check(status == 200 and len(body) > 200, f"bundled UI file {asset} (HTTP {status}, {len(body)} bytes)")

        status, about = client.json("/api/about")
        check(status == 200 and about["tracks"] == 0, f"fresh library is empty ({about.get('version')})")

        status, added = client.json("/api/library/roots", "POST", {"path": str(music)})
        check(status in (200, 201), f"add music folder (HTTP {status})")
        job = None
        for _ in range(200):
            time.sleep(0.3)
            _, job = client.json(f"/api/jobs/{added['job']['id']}")
            if job["status"] in ("done", "error", "cancelled"):
                break
        check(job is not None and job["status"] == "done" and job["result"]["added"] == len(mp3s),
              f"scan found {len(mp3s)} songs ({job and job.get('result') or job and job.get('message')})")

        status, tracks = client.json("/api/tracks?limit=10")
        check(status == 200 and tracks["total"] == len(mp3s), f"songs listed ({tracks.get('total')})")
        track = tracks["items"][0]

        status, headers, body = client.request(f"/api/art/track/{track['id']}?size=64")
        check(status == 200 and headers.get("content-type", "").startswith("image/") and len(body) > 50,
              f"cover art thumbnail via Pillow ({status} {headers.get('content-type')}, {len(body)} bytes)")
        status, headers, body = client.request(f"/api/tracks/{track['id']}/stream", headers={"Range": "bytes=0-99"})
        check(status == 206 and len(body) == 100 and headers.get("content-range", "").startswith("bytes 0-99/"),
              f"Range streaming for the player ({status} {headers.get('content-range')})")
        library_tools(client, music, len(mp3s))
        devices_and_accounts(client, scratch, len(mp3s))
        if fake_helper.exists():
            phones(client, scratch, len(mp3s))
        check(client.request(f"/api/tracks/{track['id']}/info")[0] == 200, "song details")
        check(client.request(f"/api/tracks/{track['id']}/lyrics")[0] == 200, "lyrics lookup")
        personal_layer(client, tracks["items"])
        radio_and_podcasts(client, internet_urls, tone)
        if fake_disc.exists():
            discs(client, scratch, music)
        check(client.request("/api/home")[0] == 200, "Home data")
        status, settings = client.json("/api/settings")
        check(status == 200 and "library" in settings, "settings")
        status, backup = client.json("/api/system/backup", "POST", {})
        check(status in (200, 201) and Path(backup["path"]).exists(), "database backup")

        database = profile / ".musictoolkit" / "data" / "library.db"
        check(database.exists(), f"database lives in the user profile ({database})")
        stray = [p.name for p in elsewhere.iterdir()]
        check(not stray, f"nothing written to the working directory ({stray})")
    except Exception as error:
        failures.append(f"unexpected error: {error!r}")
        raise
    finally:
        stop(process)
        disc_servers.close()
        lrclib.shutdown()
        for server in internet:
            server.shutdown()
        time.sleep(0.5)
        if failures:
            print("\n--- backend output ---")
            print(log_path.read_text(errors="replace")[-4000:])
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    exe, fixtures = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    print(f"Smoke-testing {exe}")
    run(exe, fixtures)
    if failures:
        print(f"\nFAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("\nFrozen backend smoke test passed.")


if __name__ == "__main__":
    main()
