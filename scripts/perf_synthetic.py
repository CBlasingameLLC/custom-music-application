"""Time the library screens against a synthetic library, with no audio files involved.

    python scripts/perf_synthetic.py                      # 100,000 songs, 300,000 plays
    python scripts/perf_synthetic.py --tracks 20000 --plays 50000 --budget-ms 300
    python scripts/perf_synthetic.py --db /path/to/library.db        # a copy of a real library, or one built earlier

Builds a database of invented songs and plays (a few thousand artists, a long tail of rarely played songs, three years
of listening), starts the app in-process, and asks every screen's endpoint for its data three times. Prints the first
call (nothing warmed up) and the slowest repeat for each, and exits 1 if either is over its budget: --budget-ms for
every screen, except that the first call of a Stats period may take up to --stats-first-ms (it reads the whole of that
period of listening once; the answer is then kept until the history changes). Rows are inserted directly, so building
the library takes seconds. Run it with the Python that has Music Toolkit installed.
"""

from __future__ import annotations

import argparse
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def build(db_path: Path, tracks: int, plays: int, seed: int = 7) -> None:
    rnd = random.Random(seed)
    now = datetime.now(timezone.utc)
    artists = [f"Artist {n:05d}" for n in range(max(50, tracks // 20))]
    genres = ["Rock", "Pop", "Electronic", "Jazz", "Folk", "Hip-Hop", "Classical", "Metal", "Indie", "Ambient", "Soul", "Country"]
    conn = sqlite3.connect(db_path)
    rows = []
    for n in range(tracks):
        artist = artists[int(rnd.paretovariate(1.2)) % len(artists)] if rnd.random() < 0.6 else rnd.choice(artists)
        album = f"Album {n // 11:06d}"
        added = (now - timedelta(days=rnd.randrange(0, 1100))).strftime("%Y-%m-%d %H:%M:%S")
        rows.append((f"/music/{artist}/{album}/{n:06d}.mp3", rnd.randrange(60, 480) + rnd.random(), f"Song {n:06d}", artist, artist, album,
                     n % 11 + 1, rnd.randrange(1960, 2026), rnd.choice(genres), "mp3", 192000, added, rnd.choice([0, 0, 0, 0, 3, 4, 5]) or None))
    with conn:
        conn.executemany(
            "INSERT INTO tracks (file_path, duration_seconds, title, artist, album_artist, album, track_number, year, genre, format, bitrate, date_added, rating) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        every = [(r[0], r[1]) for r in conn.execute("SELECT id, artist FROM tracks")]
        history = []
        start = int((now - timedelta(days=1095)).timestamp())
        for _ in range(plays):
            track_id, artist = every[min(len(every) - 1, int(rnd.paretovariate(1.05)) * 7 % len(every))] if rnd.random() < 0.7 else rnd.choice(every)
            history.append(("future_scrobble", rnd.randrange(start, int(now.timestamp())), rnd.randrange(30_000, 300_000), track_id, artist, f"Song {track_id:06d}"))
        conn.executemany("INSERT INTO play_history (source, played_at_epoch, ms_played, track_id, raw_artist_name, raw_track_name) VALUES (?, ?, ?, ?, ?, ?)", history)
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tracks", type=int, default=100_000)
    parser.add_argument("--plays", type=int, default=300_000)
    parser.add_argument("--budget-ms", type=float, default=450.0)
    parser.add_argument("--stats-first-ms", type=float, default=6000.0, help="how long the first call of a Stats period may take")
    parser.add_argument("--db", type=Path, help="time this database (a copy; it is upgraded if it is old) instead of building one")
    args = parser.parse_args()

    from fastapi.testclient import TestClient

    from musictoolkit.config import Config
    from musictoolkit.db.connection import connect
    from musictoolkit.web.app import create_app

    work = Path(tempfile.mkdtemp(prefix="mtk-perf-"))
    config = Config()
    config.database.path = str(work / "library.db")
    config.logging.dir = str(work / "logs")
    if args.db:
        shutil.copy(args.db, work / "library.db")
    else:
        connect(work / "library.db").close()  # create the schema
        started = time.time()
        build(work / "library.db", args.tracks, args.plays)
        print(f"built {args.tracks:,} songs and {args.plays:,} plays in {time.time() - started:.1f}s")
    started = time.time()
    with connect(work / "library.db") as opened:
        songs, plays = (opened.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("tracks", "play_history"))
    print(f"{songs:,} songs and {plays:,} plays (opened and upgraded in {time.time() - started:.1f}s)")
    client = TestClient(create_app(db_path=work / "library.db", config_path=work / "config.toml", config=config), raise_server_exceptions=True)

    def get(path: str):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.status_code} {response.text[:200]}"
        return response.json()

    year = datetime.now(timezone.utc).year
    mix_ids = [m["id"] for m in get("/api/mixes?tz=0")["items"]]
    some = [r["id"] for r in client.get("/api/tracks?limit=5&sort=plays&dir=desc").json()["items"]]
    cases = [
        ("Home", "/api/home"), ("Songs, first page", "/api/tracks?limit=100&sort=title"), ("Songs, sorted by plays", "/api/tracks?limit=100&sort=plays&dir=desc"),
        ("Albums", "/api/albums?limit=100"), ("Artists", "/api/artists?limit=100"),
        ("Stats: years", "/api/stats/years?tz=0"), ("Stats: all time", "/api/stats/overview?range=all&tz=0"),
        ("Stats: last 12 months", "/api/stats/overview?range=days:365&tz=0"), (f"Stats: {year}", f"/api/stats/overview?range=year:{year}&tz=0"),
        ("Stats: last 30 days", "/api/stats/overview?range=days:30&tz=0"),
        ("Mixes (all of Home's shelf)", "/api/mixes?tz=0"),
        *((f"Mix: {mix}", f"/api/mixes/{mix}?tz=0") for mix in mix_ids),
        *((f"Radio from song {n + 1}", f"/api/radio/track/{track}") for n, track in enumerate(some[:3])),
        ("New releases", "/api/releases"),
    ]
    over = 0
    print(f"{'screen':<34}{'first call':>12}{'slowest repeat':>17}")
    for name, path in cases:
        times = []
        for _ in range(3):
            began = time.perf_counter()
            get(path)
            times.append((time.perf_counter() - began) * 1000)
        first, repeat = times[0], max(times[1:])
        first_budget = args.stats_first_ms if name.startswith("Stats: ") and name != "Stats: years" else args.budget_ms
        late = first > first_budget or repeat > args.budget_ms
        over += late
        print(f"{name:<34}{first:>9.0f} ms{repeat:>14.0f} ms{'  <- over budget' if late else ''}")
    if over:
        sys.exit(f"\n{over} screen(s) were over budget ({args.budget_ms:.0f} ms; {args.stats_first_ms:.0f} ms for the first call of a Stats period)")
    print(f"\nevery screen answered within {args.budget_ms:.0f} ms (the first call of a Stats period within {args.stats_first_ms:.0f} ms)")


if __name__ == "__main__":
    main()
