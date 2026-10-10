"""Mixes: ready-made queues from the library and the listening history, and song radio.

A mix is a question asked of the library ("what have I been playing a lot this month?", "what used to be a
favourite that I have not heard in half a year?"). Mixes that pick at random pick the same songs all day, so a
card on Home keeps its cover and opening it twice gives the same list; tomorrow it is a new draw.
"""

from __future__ import annotations

import base64
import random
import sqlite3
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from musictoolkit.web.queries import HISTORY_JOIN, tracks_by_ids

MIN_TRACKS = 8  # fewer than this is not worth a card
DEFAULT_SIZE = 50
MAX_SIZE = 200
RECENT_DAYS = 30
FORGOTTEN_DAYS = 180
LOVED_FORGOTTEN_DAYS = 90
GENRES = 6
FROM = f"FROM tracks t {HISTORY_JOIN}"


@dataclass
class Mix:
    id: str
    title: str
    subtitle: str
    where: str
    params: list[Any] = field(default_factory=list)
    order: str | None = None  # None: a pseudo-random draw; otherwise an SQL ORDER BY
    join: str = ""  # extra tables the where and order refer to, read once rather than once per song
    join_params: list[Any] = field(default_factory=list)


def daily_seed(now: datetime) -> int:
    return int(now.timestamp() // 86400)


def _since(now: datetime, days: int) -> int:
    return int((now - timedelta(days=days)).timestamp())


def _earlier_years_around_today(conn: sqlite3.Connection, now: datetime, offset_minutes: int, spread: int = 3) -> list[tuple[int, int]]:
    """For each earlier year that has history, the epoch range from `spread` days before today's date to `spread` days after
    it, on the viewer's calendar: where to look for what was played around this date in other years."""
    zone = timezone(timedelta(minutes=offset_minutes))
    today = now.astimezone(zone).date()
    first = conn.execute("SELECT MIN(played_at_epoch) AS first FROM play_history").fetchone()["first"]
    if first is None:
        return []
    first_year = datetime.fromtimestamp(first, zone).year
    windows = []
    for year in range(first_year, today.year):
        try:
            centre = today.replace(year=year)
        except ValueError:  # the 29th of February in a year without one
            centre = today.replace(year=year, day=28)
        low = datetime.combine(centre - timedelta(days=spread), datetime.min.time(), zone)
        high = datetime.combine(centre + timedelta(days=spread + 1), datetime.min.time(), zone)
        windows.append((int(low.timestamp()), int(high.timestamp()) - 1))
    return windows


def _specs(conn: sqlite3.Connection, now: datetime, offset_minutes: int) -> list[Mix]:
    recent = _since(now, RECENT_DAYS)
    windows = _earlier_years_around_today(conn, now, offset_minutes)
    if windows:
        on_this_day = Mix(
            "on-this-day", "On this day", "What you were playing around this date in earlier years", "1 = 1", order="h.plays DESC, t.id",
            join="JOIN (SELECT DISTINCT track_id FROM play_history WHERE track_id IS NOT NULL AND (" + " OR ".join(["played_at_epoch BETWEEN ? AND ?"] * len(windows)) + ")) r ON r.track_id = t.id",
            join_params=[bound for window in windows for bound in window],
        )
    else:
        on_this_day = Mix("on-this-day", "On this day", "What you were playing around this date in earlier years", "0 = 1")
    specs = [
        Mix(
            "on-repeat", "On repeat", f"What you have kept coming back to in the last {RECENT_DAYS} days",
            "1 = 1",
            order="r.n DESC, h.last_played DESC, t.id",
            join="JOIN (SELECT track_id, COUNT(*) AS n FROM play_history WHERE track_id IS NOT NULL AND played_at_epoch >= ? GROUP BY track_id HAVING n >= 2) r ON r.track_id = t.id",
            join_params=[recent],
        ),
        Mix(
            "rediscover", "Rediscover", "Songs you loved and have not heard in a while",
            "((COALESCE(h.plays, 0) >= 3 AND (h.last_played IS NULL OR h.last_played < ?)) OR (t.favorite = 1 AND h.last_played < ?))",
            [_since(now, FORGOTTEN_DAYS), _since(now, LOVED_FORGOTTEN_DAYS)],
        ),
        on_this_day,
        Mix(
            "new-arrivals", "New in your library", f"Added in the last {RECENT_DAYS} days",
            "julianday(t.date_added) >= julianday(?)", [(now - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")],
            "t.date_added DESC, t.id",
        ),
        Mix("unheard", "Never played", "Songs in your library you have not listened to yet", "COALESCE(h.plays, 0) = 0"),
        Mix("favorites", "Your favorites", "The songs you have starred, shuffled", "t.favorite = 1"),
    ]
    for row in conn.execute(
        "SELECT (t.year / 10) * 10 AS decade, COUNT(*) AS n FROM tracks t WHERE t.is_missing = 0 AND t.year >= 1900 "
        "GROUP BY decade HAVING n >= ? ORDER BY n DESC", (MIN_TRACKS,)
    ).fetchall()[:4]:
        decade = int(row["decade"])
        specs.append(Mix(f"decade-{decade}", f"The {decade}s", "From your library, shuffled", "t.year >= ? AND t.year < ?", [decade, decade + 10]))
    for row in conn.execute(
        "SELECT t.genre AS genre, COUNT(*) AS n FROM tracks t WHERE t.is_missing = 0 AND TRIM(COALESCE(t.genre, '')) <> '' "
        "GROUP BY LOWER(t.genre) HAVING n >= ? ORDER BY n DESC LIMIT ?", (MIN_TRACKS, GENRES)
    ).fetchall():
        specs.append(Mix(genre_id(row["genre"]), row["genre"], "A shuffle of the genre", "LOWER(t.genre) = LOWER(?)", [row["genre"]]))
    return specs


def _from(spec: Mix) -> str:
    return f"FROM tracks t {HISTORY_JOIN} {spec.join} WHERE t.is_missing = 0 AND {spec.where}"


def _count(conn: sqlite3.Connection, spec: Mix, cap: int) -> int:
    """How many songs the mix has, counted only up to `cap` (a card says "50 songs" for a mix of five thousand)."""
    return conn.execute(f"SELECT COUNT(*) AS n FROM (SELECT 1 {_from(spec)} LIMIT ?)", [*spec.join_params, *spec.params, cap]).fetchone()["n"]


def _salt(mix_id: str, seed: int) -> int:
    return (seed * 1009 + zlib.crc32(mix_id.encode("utf-8"))) % 1_000_003


def _ids(conn: sqlite3.Connection, spec: Mix, size: int, seed: int) -> list[int]:
    """The songs of a mix in order. A mix with no order of its own is a pseudo-random draw: the songs sorted by a
    multiplicative hash of their id and the day's seed, so the same day gives the same songs and another day another set,
    without reading every song into memory to sample from."""
    order = spec.order or "((t.id + ?) * 2654435761) % 4294967296"
    order_params = [] if spec.order else [_salt(spec.id, seed)]
    rows = conn.execute(f"SELECT t.id {_from(spec)} ORDER BY {order} LIMIT ?", [*spec.join_params, *spec.params, *order_params, size]).fetchall()
    return [r["id"] for r in rows]


def available(conn: sqlite3.Connection, now: datetime | None = None, offset_minutes: int = 0, size: int = DEFAULT_SIZE, minimum: int = MIN_TRACKS) -> list[dict[str, Any]]:
    """The mixes worth showing today, each with how many songs it holds and a song to take the cover from."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    seed = daily_seed(now)
    found = []
    for spec in _specs(conn, now, offset_minutes):
        count = _count(conn, spec, size)
        if count < minimum:
            continue
        cover = _ids(conn, spec, 1, seed)
        found.append({"id": spec.id, "title": spec.title, "subtitle": spec.subtitle, "count": count, "cover_track_id": cover[0] if cover else None})
    return found


def build(conn: sqlite3.Connection, mix_id: str, now: datetime | None = None, offset_minutes: int = 0, size: int = DEFAULT_SIZE, seed: int | None = None) -> dict[str, Any] | None:
    """One mix with its songs in play order, or None if there is no such mix (or it is empty today)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    size = max(1, min(int(size), MAX_SIZE))
    chosen = next((s for s in _specs(conn, now, offset_minutes) if s.id == mix_id), None)
    if chosen is None:
        return None
    ids = _ids(conn, chosen, size, daily_seed(now) if seed is None else seed)
    if not ids:
        return None
    return {"id": chosen.id, "title": chosen.title, "subtitle": chosen.subtitle, "tracks": tracks_by_ids(conn, ids)}


def genre_id(genre: str) -> str:
    """A mix id that is safe in a URL whatever the genre is called (letters, digits, - and _ only)."""
    return "genre-" + base64.urlsafe_b64encode(genre.encode("utf-8")).decode("ascii").rstrip("=")


def genre_of(mix_id: str) -> str | None:
    if not mix_id.startswith("genre-"):
        return None
    encoded = mix_id[len("genre-"):]
    try:
        text = base64.b64decode((encoded + "=" * (-len(encoded) % 4)).replace("-", "+").replace("_", "/"), validate=True).decode("utf-8")
    except ValueError:  # not base64, or not text
        return None
    return text or None


# ------------------------------------------------------------------------------------------- song radio

CO_LISTEN_SECONDS = 20 * 60  # songs played within twenty minutes of the seed belong to the same sitting
CO_LISTEN_PLAYS = 400  # the seed's most recent plays that are looked at
CO_LISTEN_SONGS = 500  # the songs most often played with it that are kept
CANDIDATES_PER_PLACE = 12  # best-scoring songs read for each place in the radio, so the artist limit has room to work


def radio(conn: sqlite3.Connection, track_id: int, size: int = DEFAULT_SIZE, seed: int | None = None) -> list[dict[str, Any]]:
    """Songs that go with one song: the same sitting in your history, the same genre and era, the same artist (a few),
    and what you tend to like. Returns the seed first, then the rest in an order that does not repeat an artist back to back."""
    size = max(2, min(int(size), MAX_SIZE))
    seed_row = conn.execute(
        "SELECT id, artist, album_artist, genre, year FROM tracks WHERE id = ? AND is_missing = 0", (track_id,)
    ).fetchone()
    if seed_row is None:
        return []
    seed_artist = (seed_row["artist"] or seed_row["album_artist"] or "").strip().lower()

    # Which songs were played in the same sittings as the seed: looked at for the seed's most recent plays only (how
    # you listen now, and a bounded amount of work however often it was played), and the most often together kept.
    together: dict[int, int] = {}
    for row in conn.execute(
        "SELECT o.track_id AS id, COUNT(*) AS n "
        "FROM (SELECT played_at_epoch FROM play_history WHERE track_id = ? ORDER BY played_at_epoch DESC LIMIT ?) s "
        "JOIN play_history o ON o.track_id IS NOT NULL AND o.track_id <> ? AND o.played_at_epoch BETWEEN s.played_at_epoch - ? AND s.played_at_epoch + ? "
        "GROUP BY o.track_id ORDER BY n DESC LIMIT ?",
        (track_id, CO_LISTEN_PLAYS, track_id, CO_LISTEN_SECONDS, CO_LISTEN_SECONDS, CO_LISTEN_SONGS),
    ):
        together[row["id"]] = row["n"]
    most_together = max(together.values(), default=0)

    # Everything but the "played together" bonus is scored by the database, which hands back only the best few hundred
    # (a library of a hundred thousand songs is not read into Python to score one by one). The songs that were played
    # together get their bonus here and join the candidates: they are few, and nothing else has that bonus.
    rng = random.Random(f"radio:{track_id}:{seed}")
    wanted = size * CANDIDATES_PER_PLACE
    year = int(seed_row["year"]) if seed_row["year"] else None
    raw_genre = seed_row["genre"] or ""  # compared inside the database with its own LOWER, on both sides, so non-ASCII names agree
    raw_artist = seed_row["artist"] or seed_row["album_artist"] or ""
    columns = (
        "t.id, t.artist, t.album_artist, t.genre, t.year, t.favorite, COALESCE(t.rating, 0) AS rating, "
        "( CASE WHEN TRIM(?) <> '' AND LOWER(TRIM(COALESCE(t.genre, ''))) = LOWER(TRIM(?)) THEN 3 ELSE 0 END"
        "  + CASE WHEN ? IS NULL OR t.year IS NULL THEN 0 WHEN ABS(? - t.year) <= 3 THEN 2 WHEN ABS(? - t.year) <= 8 THEN 1 ELSE 0 END"
        "  + CASE WHEN TRIM(?) <> '' AND LOWER(TRIM(COALESCE(NULLIF(t.artist, ''), t.album_artist, ''))) = LOWER(TRIM(?)) THEN 2.5 ELSE 0 END"
        "  + CASE WHEN t.favorite = 1 OR COALESCE(t.rating, 0) >= 4 THEN 1 ELSE 0 END ) AS partial"
    )
    salt = _salt(f"radio:{track_id}", seed if seed is not None else 0)
    best = conn.execute(
        f"SELECT {columns} FROM tracks t WHERE t.is_missing = 0 AND t.id <> ? ORDER BY partial DESC, ((t.id + ?) * 2654435761) % 4294967296 LIMIT ?",
        (raw_genre, raw_genre, year, year, year, raw_artist, raw_artist, track_id, salt, wanted),
    ).fetchall()
    chosen = {row["id"]: row for row in best}
    if together:
        marks = ",".join("?" * len(together))
        for row in conn.execute(f"SELECT {columns} FROM tracks t WHERE t.is_missing = 0 AND t.id IN ({marks})",
                                (raw_genre, raw_genre, year, year, year, raw_artist, raw_artist, *together)):
            chosen[row["id"]] = row
    scored: list[tuple[float, int, str]] = []
    for row in chosen.values():
        score = row["partial"] + (4 * together[row["id"]] / most_together + 2 if row["id"] in together else 0)
        if score > 0:
            scored.append((score + rng.random() * 0.5, row["id"], (row["artist"] or row["album_artist"] or "").strip().lower()))
    scored.sort(reverse=True)

    # Spread the artists: no more than a third of the radio from one, and never the same artist twice in a row.
    per_artist: dict[str, int] = {}
    picked: list[tuple[int, str]] = []
    cap = max(2, size // 3)
    for _, candidate, artist in scored:
        if per_artist.get(artist, 0) >= cap:
            continue
        per_artist[artist] = per_artist.get(artist, 0) + 1
        picked.append((candidate, artist))
        if len(picked) >= size - 1:
            break
    ordered: list[int] = [track_id]
    last_artist = seed_artist
    waiting = list(picked)
    while waiting:
        index = next((i for i, (_, artist) in enumerate(waiting) if artist != last_artist), 0)
        candidate, artist = waiting.pop(index)
        ordered.append(candidate)
        last_artist = artist
    return tracks_by_ids(conn, ordered)
