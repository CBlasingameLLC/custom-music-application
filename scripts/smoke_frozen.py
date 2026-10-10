"""Smoke-test the frozen backend the way a freshly installed app starts it.

    python scripts/smoke_frozen.py dist/mtk-backend.exe tests/fixtures

Fresh user profile, an unrelated working directory, no config, no database.
Launches `<exe> dashboard`, then checks that the bundled web UI is served and
that the token gate, library scan, cover art (Pillow), Range streaming (what
the in-app player uses), the library tools (organize with undo, duplicates with
the review folder and the Recycle Bin), settings and backup all work from the
frozen binary, and that nothing leaks into the working directory.

Standard library only, so it runs on any CI runner without installing anything.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
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
    env = {**os.environ, "HOME": str(profile), "USERPROFILE": str(profile), "MTK_TOKEN": TOKEN}
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
        check(client.request(f"/api/tracks/{track['id']}/info")[0] == 200, "song details")
        check(client.request(f"/api/tracks/{track['id']}/lyrics")[0] == 200, "lyrics lookup")
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
