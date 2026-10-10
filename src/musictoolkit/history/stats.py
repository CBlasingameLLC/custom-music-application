"""Listening statistics from the play history: how much, what, and when.

Every play in `play_history` counts, whether it came from the in-app player or an imported Spotify history, and
whether or not the song is in the library any more (the names are copied into each play). Times are the viewer's:
the caller passes the offset of their time zone and plays are bucketed by local day, hour and weekday. A fixed
offset is exact except for plays on the far side of a daylight saving change, where an hour may land in the
neighbouring bucket; that does not matter for what these numbers are used for.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

MAX_OFFSET_MINUTES = 14 * 60
RANGE_PATTERN = re.compile(r"^(all|year:\d{4}|month:\d{4}-(0[1-9]|1[0-2])|days:\d{1,5})$")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_LIMIT = 50


class BadRange(ValueError):
    """The requested period is not one of: all, year:2025, month:2025-06, days:30."""


def clamp_offset(minutes: int) -> int:
    """The viewer's offset from UTC in minutes (east positive), kept to what exists on Earth."""
    return max(-MAX_OFFSET_MINUTES, min(MAX_OFFSET_MINUTES, int(minutes)))


def resolve_range(spec: str, offset_minutes: int, now: datetime | None = None) -> tuple[int, int, str]:
    """(start epoch, end epoch exclusive, title) for a period, in the viewer's time zone."""
    if not RANGE_PATTERN.match(spec or ""):
        raise BadRange("period must be all, year:2025, month:2025-06 or days:30")
    zone = timezone(timedelta(minutes=clamp_offset(offset_minutes)))
    now = (now or datetime.now(timezone.utc)).astimezone(zone)

    def at(year: int, month: int = 1) -> int:
        return int(datetime(year, month, 1, tzinfo=zone).timestamp())

    kind, _, value = spec.partition(":")
    if kind == "all":
        return 0, int(now.timestamp()) + 86400, "All time"
    if kind == "year":
        year = int(value)
        return at(year), at(year + 1), str(year)
    if kind == "month":
        year, month = (int(part) for part in value.split("-"))
        end = at(year + 1) if month == 12 else at(year, month + 1)
        return at(year, month), end, datetime(year, month, 1).strftime("%B %Y")
    days = max(1, int(value))
    return int((now - timedelta(days=days)).timestamp()), int(now.timestamp()) + 1, f"The last {days} days"


# One row per play in the period, with the names the play carries (or the song's tags if it carries none).
NAMED_PLAYS = """
SELECT h.track_id AS track_id, h.played_at_epoch AS at, h.played_at_epoch + :off AS ts, COALESCE(h.ms_played, 0) AS ms,
       COALESCE(NULLIF(TRIM(h.raw_artist_name), ''), NULLIF(TRIM(t.artist), '')) AS artist,
       COALESCE(NULLIF(TRIM(h.raw_track_name), ''), NULLIF(TRIM(t.title), '')) AS title,
       COALESCE(NULLIF(TRIM(h.raw_album_name), ''), NULLIF(TRIM(t.album), '')) AS album,
       NULLIF(TRIM(t.genre), '') AS genre
FROM play_history h LEFT JOIN tracks t ON t.id = h.track_id
WHERE h.played_at_epoch >= :start AND h.played_at_epoch < :end
"""
# The same plays grouped once by artist, song, album and genre (names compared without regard to case). Every ranking
# below reads this table of a few tens of thousands of rows instead of grouping every play again: on a long history
# that is the difference between seconds and a fraction of a second.
GROUPED = f"""
CREATE TEMP TABLE pg AS
SELECT LOWER(artist) AS ak, LOWER(title) AS tk, LOWER(album) AS bk, LOWER(genre) AS gk,
       MIN(artist) AS artist, MIN(title) AS title, MIN(album) AS album, MIN(genre) AS genre,
       COUNT(*) AS n, SUM(ms) AS ms, MAX(track_id) AS track_id, MIN(at) AS first
FROM ({NAMED_PLAYS}) GROUP BY ak, tk, bk, gk
"""
FAVORITE_MONTHS = 12  # "song of each month" looks back this far, however long the period is
MIN_MONTHS_FOR_FAVORITES = 3  # it needs months to compare; the screen shows it from three on


def _limit(value: int) -> int:
    return max(1, min(int(value), MAX_LIMIT))


def years(conn: sqlite3.Connection, offset_minutes: int) -> list[dict[str, int]]:
    """The calendar years with at least one play, newest first."""
    rows = conn.execute(
        "SELECT strftime('%Y', played_at_epoch + ?, 'unixepoch') AS year, COUNT(*) AS plays FROM play_history "
        "GROUP BY year ORDER BY year DESC",
        (clamp_offset(offset_minutes) * 60,),
    ).fetchall()
    return [{"year": int(r["year"]), "plays": r["plays"]} for r in rows]


def heard_before(conn: sqlite3.Connection, keys: list[str], start: int) -> set[str]:
    """Which of these artists (lower-cased, as the figures group them) have a play before `start`. A play carries the
    name it was played under, and an index on that name makes each question a single lookup."""
    return {
        key for key in keys
        if conn.execute("SELECT 1 FROM play_history WHERE LOWER(TRIM(raw_artist_name)) = ? AND played_at_epoch < ? LIMIT 1", (key, start)).fetchone()
    }


def _longest_streak(days: list[str]) -> dict[str, Any]:
    best_len, best_end, run_len, previous = 0, None, 0, None
    for text in days:
        day = date.fromisoformat(text)
        run_len = run_len + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        if run_len > best_len:
            best_len, best_end = run_len, day
        previous = day
    if not best_len:
        return {"days": 0, "from": None, "to": None}
    return {"days": best_len, "from": (best_end - timedelta(days=best_len - 1)).isoformat(), "to": best_end.isoformat()}


def _month_keys(first: str, last: str) -> list[str]:
    year, month = (int(part) for part in first.split("-"))
    end_year, end_month = (int(part) for part in last.split("-"))
    keys = []
    while (year, month) <= (end_year, end_month):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def overview(conn: sqlite3.Connection, spec: str = "all", offset_minutes: int = 0, limit: int = 10, now: datetime | None = None) -> dict[str, Any]:
    """Everything the Stats screen shows for one period."""
    offset = clamp_offset(offset_minutes)
    start, end, title = resolve_range(spec, offset, now)
    limit = _limit(limit)
    args = {"off": offset * 60, "start": start, "end": end}
    result: dict[str, Any] = {"range": spec, "title": title, "start": start, "end": end, "offset_minutes": offset}

    # When: two cheap groupings of the plays alone (no join, whole-number arithmetic): by day, and by hour of the day.
    # The month and weekday figures and the streak come from the days.
    by_day_rows = conn.execute(
        "SELECT (played_at_epoch + :off) / 86400 AS day, COUNT(*) AS plays, SUM(COALESCE(ms_played, 0)) AS ms, MIN(played_at_epoch) AS first, MAX(played_at_epoch) AS last "
        "FROM play_history WHERE played_at_epoch >= :start AND played_at_epoch < :end GROUP BY day", args).fetchall()
    if not by_day_rows:
        return {**result, "empty": True,
                "totals": {"plays": 0, "minutes": 0, "artists": 0, "tracks": 0, "active_days": 0, "first_play": None, "last_play": None},
                "top_artists": [], "top_tracks": [], "top_albums": [], "top_genres": [], "by_month": [], "by_hour": [0] * 24, "by_weekday": [0] * 7,
                "by_day": [], "streak": _longest_streak([]), "discoveries": {"count": 0, "items": []}, "month_favorites": []}
    day_plays: dict[str, int] = {}
    month_plays: dict[str, list[int]] = {}
    by_weekday = [0] * 7
    epoch_day = date(1970, 1, 1)
    for r in by_day_rows:
        day = epoch_day + timedelta(days=r["day"])  # a whole number of days since 1970 is the viewer's calendar day
        text = day.isoformat()
        day_plays[text] = r["plays"]
        month = month_plays.setdefault(text[:7], [0, 0])
        month[0] += r["plays"]
        month[1] += r["ms"]
        by_weekday[day.weekday()] += r["plays"]  # Monday first
    by_hour = [0] * 24
    for r in conn.execute(
        "SELECT ((played_at_epoch + :off) % 86400) / 3600 AS hour, COUNT(*) AS plays FROM play_history "
        "WHERE played_at_epoch >= :start AND played_at_epoch < :end GROUP BY hour", args):
        by_hour[r["hour"]] = r["plays"]
    days = sorted(day_plays)
    ordered = sorted(month_plays)
    by_month = [
        {"month": key, "plays": month_plays[key][0] if key in month_plays else 0, "minutes": round(month_plays[key][1] / 60000) if key in month_plays else 0}
        for key in _month_keys(ordered[0], ordered[-1])
    ]
    result.update(
        by_month=by_month, by_hour=by_hour, by_weekday=by_weekday, streak=_longest_streak(days),
        by_day=[{"date": d, "plays": day_plays[d]} for d in days] if (end - start) <= 370 * 86400 else [],
    )

    # What: the names, grouped once and ranked from the groups.
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -65536")  # the join to the songs thrashes the default 2 MB page cache on a big library
    conn.execute("DROP TABLE IF EXISTS temp.pg")
    conn.execute(GROUPED, args)
    try:
        def rows(sql: str, extra: dict[str, Any] | None = None) -> list[sqlite3.Row]:
            return conn.execute(sql, extra or {}).fetchall()

        distinct_artists = rows("SELECT COUNT(DISTINCT ak) AS n FROM pg")[0]["n"]
        distinct_tracks = rows("SELECT COUNT(*) AS n FROM (SELECT 1 FROM pg WHERE ak IS NOT NULL AND tk IS NOT NULL GROUP BY ak, tk)")[0]["n"]
        result["totals"] = {
            "plays": sum(r["plays"] for r in by_day_rows), "minutes": round(sum(r["ms"] for r in by_day_rows) / 60000), "artists": distinct_artists,
            "tracks": distinct_tracks, "active_days": len(days), "first_play": min(r["first"] for r in by_day_rows), "last_play": max(r["last"] for r in by_day_rows),
        }
        result["empty"] = False
        result["top_artists"] = [
            {"name": r["name"], "plays": r["plays"], "minutes": round(r["ms"] / 60000), "track_id": r["track_id"]}
            for r in rows(
                "SELECT MIN(artist) AS name, SUM(n) AS plays, SUM(ms) AS ms, MAX(track_id) AS track_id FROM pg WHERE ak IS NOT NULL "
                "GROUP BY ak ORDER BY plays DESC, ms DESC, name LIMIT :n", {"n": limit})
        ]
        result["top_tracks"] = [
            {"title": r["title"], "artist": r["artist"], "album": r["album"], "plays": r["plays"], "minutes": round(r["ms"] / 60000), "track_id": r["track_id"]}
            for r in rows(
                "SELECT MIN(title) AS title, MIN(artist) AS artist, MIN(album) AS album, SUM(n) AS plays, SUM(ms) AS ms, MAX(track_id) AS track_id "
                "FROM pg WHERE title IS NOT NULL GROUP BY ak, tk ORDER BY plays DESC, ms DESC, title LIMIT :n", {"n": limit})
        ]
        result["top_albums"] = [
            {"album": r["album"], "artist": r["artist"], "plays": r["plays"], "track_id": r["track_id"]}
            for r in rows(
                "SELECT MIN(album) AS album, MIN(artist) AS artist, SUM(n) AS plays, MAX(track_id) AS track_id FROM pg WHERE album IS NOT NULL "
                "GROUP BY ak, bk ORDER BY plays DESC, album LIMIT :n", {"n": limit})
        ]
        result["top_genres"] = [
            {"genre": r["genre"], "plays": r["plays"]}
            for r in rows("SELECT MIN(genre) AS genre, SUM(n) AS plays FROM pg WHERE genre IS NOT NULL GROUP BY gk ORDER BY plays DESC, genre LIMIT :n", {"n": limit})
        ]

        # Artists heard for the first time in this period (every artist is new over all time, so nothing to ask there).
        candidates = rows("SELECT ak AS key, MIN(artist) AS name, MIN(first) AS first, SUM(n) AS plays FROM pg WHERE ak IS NOT NULL GROUP BY ak ORDER BY plays DESC, name")
        if start > 0 and conn.execute("SELECT 1 FROM play_history WHERE played_at_epoch < ? LIMIT 1", (start,)).fetchone():
            heard = heard_before(conn, [r["key"] for r in candidates], start)
            candidates = [r for r in candidates if r["key"] not in heard]
        result["discoveries"] = {"count": len(candidates), "items": [{"name": r["name"], "plays": r["plays"], "first_play": r["first"]} for r in candidates[:limit]]}
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.pg")

    result["month_favorites"] = [] if len(by_month) < MIN_MONTHS_FOR_FAVORITES else _month_favorites(conn, by_month[-FAVORITE_MONTHS:][0]["month"], offset, args)
    return result


def _month_favorites(conn: sqlite3.Connection, first_month: str, offset: int, args: dict[str, int]) -> list[dict[str, Any]]:
    """The most played song of each month, from `first_month` on."""
    year, month = (int(part) for part in first_month.split("-"))
    from_epoch = max(args["start"], int(datetime(year, month, 1, tzinfo=timezone(timedelta(minutes=offset))).timestamp()))
    return [
        {"month": r["month"], "title": r["title"], "artist": r["artist"], "plays": r["plays"], "track_id": r["track_id"]}
        for r in conn.execute(
            "SELECT month, title, artist, plays, track_id FROM ("
            " SELECT strftime('%Y-%m', ts, 'unixepoch') AS month, MIN(title) AS title, MIN(artist) AS artist, COUNT(*) AS plays, MAX(track_id) AS track_id,"
            "  ROW_NUMBER() OVER (PARTITION BY strftime('%Y-%m', ts, 'unixepoch') ORDER BY COUNT(*) DESC, MIN(title)) AS rank"
            f" FROM ({NAMED_PLAYS}) WHERE title IS NOT NULL GROUP BY month, LOWER(artist), LOWER(title)) WHERE rank = 1 ORDER BY month",
            {**args, "start": from_epoch})
    ]
