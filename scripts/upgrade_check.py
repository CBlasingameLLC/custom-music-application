"""Check that library data made by one version of the app is intact after upgrading to another.

    python scripts/upgrade_check.py seed   <backend.exe> <profile-dir> <music-dir> <facts.json>
    python scripts/upgrade_check.py verify <backend.exe> <profile-dir> <facts.json>

CI installs the previous release, runs `seed` with the backend it ships (a fresh user profile, a music
folder scanned, a rating, a favorite, plays, a playlist and a setting), installs the new build over it,
then runs `verify` with the new backend on the same profile: nothing may be lost, the database must
have been migrated, and a rescan must find the library unchanged.

Only API calls that every release has had since 0.2.0 are used for seeding, because the seeding backend
is an old build. Standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_frozen import TOKEN, WINDOWS, Client, check, failures, free_port, stop, wait_job  # noqa: E402

CONTACT = "upgrade-check@example.com"


def start_backend(exe: Path, profile: Path, scratch: Path) -> tuple[subprocess.Popen, Client, Path]:
    port = free_port()
    log_path = scratch / f"backend-{int(time.time())}.log"
    env = {**os.environ, "HOME": str(profile), "USERPROFILE": str(profile), "MTK_TOKEN": TOKEN}
    with open(log_path, "w") as log:
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
        process = subprocess.Popen(
            [str(exe), "dashboard", "--port", str(port)], cwd=scratch, env=env, stdout=log, stderr=subprocess.STDOUT, **kwargs
        )
    client = Client(f"http://127.0.0.1:{port}")
    for _ in range(240):
        if process.poll() is not None:
            break
        try:
            urllib.request.urlopen(client.base + "/", timeout=2)
        except urllib.error.HTTPError:
            break  # 401 without the token: the server is up
        except OSError:
            time.sleep(0.5)
    check(process.poll() is None, f"backend {exe.name} is running")
    return process, client, log_path


def seed(exe: Path, profile: Path, music_source: Path, facts_path: Path) -> None:
    scratch = Path(tempfile.mkdtemp(prefix="mtk-seed-"))
    music = scratch / "music"
    music.mkdir()
    for mp3 in sorted(music_source.glob("*.mp3")):
        shutil.copy(mp3, music / mp3.name)
    profile.mkdir(parents=True, exist_ok=True)
    process, client, log_path = start_backend(exe, profile, scratch)
    try:
        if process.poll() is not None:
            return
        _, about = client.json("/api/about")
        status, added = client.json("/api/library/roots", "POST", {"path": str(music)})
        job = wait_job(client, added["job"]["id"])
        check(job["status"] == "done", f"the old version scanned the music ({job.get('result') or job.get('error')})")
        _, listing = client.json("/api/tracks?limit=50")
        tracks = listing["items"]
        check(len(tracks) >= 3, f"{len(tracks)} songs in the old library")
        first, second = tracks[0], tracks[1]

        client.json(f"/api/tracks/{first['id']}", "PATCH", {"rating": 4})
        client.json(f"/api/tracks/{second['id']}", "PATCH", {"favorite": True})
        for _ in range(2):
            client.json("/api/plays", "POST", {"track_id": first["id"], "ms_played": 30000})
        status, playlist = client.json("/api/playlists", "POST", {"name": "Upgrade check", "track_ids": [second["id"], first["id"]]})
        check(status == 201, f"a playlist was created (HTTP {status})")
        status, _ = client.json("/api/settings", "PUT", {"musicbrainz": {"contact": CONTACT}})
        check(status == 200, "a setting was saved")

        facts = {
            "old_version": about["version"], "music": str(music), "tracks": listing["total"],
            "first": {"id": first["id"], "title": first["title"], "rating": 4, "plays": 2},
            "second": {"id": second["id"], "title": second["title"], "favorite": True},
            "playlist": {"id": playlist["id"], "name": "Upgrade check", "order": [second["id"], first["id"]]},
        }
        facts_path.write_text(json.dumps(facts, indent=2))
        print(f"seeded {facts['tracks']} songs with {about['version']}")
    finally:
        stop(process)
        time.sleep(1)
        if failures:
            print(log_path.read_text(errors="replace")[-3000:])


def verify(exe: Path, profile: Path, facts_path: Path) -> None:
    facts = json.loads(facts_path.read_text())
    scratch = Path(tempfile.mkdtemp(prefix="mtk-verify-"))
    process, client, log_path = start_backend(exe, profile, scratch)
    try:
        if process.poll() is not None:
            return
        _, about = client.json("/api/about")
        check(about["version"] != facts["old_version"] or bool(os.environ.get("MTK_UPGRADE_ALLOW_SAME")),
              f"the new backend is a different version ({facts['old_version']} -> {about['version']})")
        check(about["tracks"] == facts["tracks"], f"all {facts['tracks']} songs survived the upgrade ({about['tracks']})")

        first = client.json(f"/api/tracks/{facts['first']['id']}")[1]
        second = client.json(f"/api/tracks/{facts['second']['id']}")[1]
        check(first["rating"] == facts["first"]["rating"] and first["title"] == facts["first"]["title"], f"the rating survived ({first.get('rating')})")
        check(first["plays"] == facts["first"]["plays"], f"the play count survived ({first.get('plays')})")
        check(bool(second["favorite"]), "the favorite survived")

        _, ordered = client.json("/api/tracks?" + urllib.parse.urlencode({"playlist": facts["playlist"]["id"], "limit": 50}))
        check([t["id"] for t in ordered["items"]] == facts["playlist"]["order"], "the playlist survived with its order")
        _, settings = client.json("/api/settings")
        check(settings["musicbrainz"]["contact"] == CONTACT, "the setting survived")

        # the new schema: tables that only exist after the newer migrations
        for path in ("/api/tools/organize/batches", "/api/tools/duplicates/info", "/api/tools/missing/summary",
                     "/api/stats/years", "/api/mixes", "/api/releases"):
            status, _ = client.json(path)
            check(status == 200, f"{path} works on the migrated database (HTTP {status})")

        # Home reads the per-song play counts that the migration worked out from the old history, and the indexes it added
        status, home = client.json("/api/home")
        check(status == 200 and [t["id"] for t in home["most_played"]][:1] == [facts["first"]["id"]], "Home lists the most played song after the upgrade")
        check(status == 200 and [t["id"] for t in home["favorites"]] == [facts["second"]["id"]], "Home lists the favorite after the upgrade")
        client.json("/api/plays", "POST", {"track_id": facts["first"]["id"], "ms_played": 30000})
        counted = client.json(f"/api/tracks/{facts['first']['id']}")[1]
        check(counted["plays"] == facts["first"]["plays"] + 1, f"a new play is counted on the migrated database ({counted.get('plays')})")

        _, started = client.json("/api/library/scan", "POST")
        scan = wait_job(client, started["job"]["id"])
        result = scan.get("result") or {}
        check(scan["status"] == "done" and result.get("added") == 0 and result.get("missing") == 0,
              f"a rescan finds the same library ({result})")
        status, backup = client.json("/api/system/backup", "POST", {})
        check(status in (200, 201), "a backup of the migrated database works")
    finally:
        stop(process)
        time.sleep(1)
        if failures:
            print(log_path.read_text(errors="replace")[-3000:])


def main() -> None:
    args = sys.argv[1:]
    if len(args) == 5 and args[0] == "seed":
        seed(Path(args[1]).resolve(), Path(args[2]).resolve(), Path(args[3]).resolve(), Path(args[4]).resolve())
    elif len(args) == 4 and args[0] == "verify":
        verify(Path(args[1]).resolve(), Path(args[2]).resolve(), Path(args[3]).resolve())
    else:
        sys.exit(__doc__)
    if failures:
        print(f"\nFAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("\nOK")


if __name__ == "__main__":
    main()
