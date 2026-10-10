"""SQL building for the library views: filters, search, sorting, row shapes.

Everything user-controlled is either bound as a parameter or looked up in a
whitelist below — no request value is ever interpolated into SQL.
"""

from __future__ import annotations

import base64
import json
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException

UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"

ALBUM_ARTIST_SQL = "COALESCE(NULLIF(t.album_artist, ''), NULLIF(t.artist, ''), 'Unknown Artist')"
ALBUM_SQL = "COALESCE(NULLIF(t.album, ''), 'Unknown Album')"
ARTIST_SQL = "COALESCE(NULLIF(t.artist, ''), NULLIF(t.album_artist, ''), 'Unknown Artist')"

# The same two names as ALBUM_ARTIST_SQL / ALBUM_SQL, spelt as idx_tracks_album_key spells them (migration 0010 says why):
# for finding the songs of one album, never for grouping.
ALBUM_ARTIST_FIND = "IFNULL(NULLIF(t.album_artist, ''), IFNULL(NULLIF(t.artist, ''), 'Unknown Artist'))"
ALBUM_FIND = "IFNULL(NULLIF(t.album, ''), 'Unknown Album')"

# plays and last_played for each song: one row per song, kept up to date by triggers (migration 0009)
HISTORY_JOIN = """
LEFT JOIN track_plays h ON h.track_id = t.id
"""

TRACK_COLUMNS = """
  t.id, t.title, t.artist, t.album_artist, t.album, t.track_number, t.disc_number, t.year, t.genre,
  t.duration_seconds, t.rating, t.favorite, t.format, t.bitrate, t.date_added, t.file_path,
  COALESCE(h.plays, 0) AS plays, h.last_played
"""

SEARCH_HAYSTACK = (
    "LOWER(COALESCE(NULLIF(t.title, ''), t.file_path) || ' ' || COALESCE(t.artist, '') || ' ' || "
    "COALESCE(t.album_artist, '') || ' ' || COALESCE(t.album, '') || ' ' || COALESCE(t.genre, '') || ' ' || "
    "COALESCE(t.year, ''))"
)

# --- filter rules ---------------------------------------------------------

TEXT_FIELDS = {
    "title": "t.title",
    "artist": "t.artist",
    "album_artist": "t.album_artist",
    "album": "t.album",
    "genre": "t.genre",
    "format": "t.format",
    "path": "t.file_path",
}

NUM_FIELDS = {
    "year": "t.year",
    "rating": "COALESCE(t.rating, 0)",
    "bitrate": "(t.bitrate / 1000.0)",  # kbps
    "duration": "t.duration_seconds",  # seconds
    "plays": "COALESCE(h.plays, 0)",
    "track": "t.track_number",
}

# Predicates with a numeric argument and no operator: {"field": ..., "op": "is", "value": 30}
DAY_FIELDS = {
    "added_within_days": "julianday(t.date_added) >= julianday('now') - ?",
    "played_within_days": "h.last_played >= CAST(strftime('%s', 'now') AS INTEGER) - ? * 86400",
    "not_played_within_days": (
        "(h.last_played IS NULL OR h.last_played < CAST(strftime('%s', 'now') AS INTEGER) - ? * 86400)"
    ),
}

# Predicates with a boolean argument: {"field": "favorite", "op": "is", "value": true}
BOOL_FIELDS = {
    "favorite": "t.favorite = 1",
    "unplayed": "COALESCE(h.plays, 0) = 0",
    "missing_tags": (
        "(COALESCE(t.title, '') = '' OR COALESCE(t.artist, '') = '' OR COALESCE(t.album, '') = '')"
    ),
}

TEXT_OPS = {"is", "is_not", "is_any", "contains", "not_contains", "starts_with", "empty", "not_empty"}
NUM_OPS = {"=", "!=", ">", ">=", "<", "<=", "between"}

RULE_FIELD_NAMES = sorted({*TEXT_FIELDS, *NUM_FIELDS, *DAY_FIELDS, *BOOL_FIELDS})


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def _like(value: str, mode: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").lower()
    return {"contains": f"%{escaped}%", "starts_with": f"{escaped}%"}[mode]


def _number(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise _bad(f"{what} needs a number, got {value!r}") from None


def build_rule_sql(rules: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """Turn {"match": "all"|"any", "rules": [...]} into (SQL fragment, params)."""
    if not rules or not rules.get("rules"):
        return "", []
    joiner = " OR " if rules.get("match") == "any" else " AND "
    clauses: list[str] = []
    params: list[Any] = []

    for rule in rules["rules"]:
        if not isinstance(rule, dict):
            raise _bad("each rule must be an object")
        name, op, value = rule.get("field"), rule.get("op", "is"), rule.get("value")
        if not isinstance(name, str) or not isinstance(op, str):
            raise _bad("each rule needs a text 'field' and 'op'")

        if name in TEXT_FIELDS:
            expr = TEXT_FIELDS[name]
            if op not in TEXT_OPS:
                raise _bad(f"'{op}' is not a valid operator for {name}")
            if op == "empty":
                clauses.append(f"({expr} IS NULL OR {expr} = '')")
            elif op == "not_empty":
                clauses.append(f"({expr} IS NOT NULL AND {expr} <> '')")
            elif op == "is_any":
                if not isinstance(value, (list, tuple)) or not value:
                    raise _bad(f"{name} 'is_any' needs a list with at least one value")
                values = [str(v).lower() for v in value]
                clauses.append(f"LOWER({expr}) IN ({', '.join('?' * len(values))})")
                params.extend(values)
            elif op in ("is", "is_not"):
                clauses.append(f"LOWER({expr}) {'=' if op == 'is' else '<>'} ?")
                params.append(str(value or "").lower())
            else:  # contains / not_contains / starts_with
                mode = "contains" if op == "not_contains" else op
                negate = "NOT " if op == "not_contains" else ""
                clauses.append(f"{negate}COALESCE(LOWER({expr}), '') LIKE ? ESCAPE '\\'")
                params.append(_like(str(value or ""), mode))

        elif name in NUM_FIELDS:
            expr = NUM_FIELDS[name]
            if op not in NUM_OPS:
                raise _bad(f"'{op}' is not a valid operator for {name}")
            if op == "between":
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise _bad(f"{name} 'between' needs [low, high]")
                clauses.append(f"{expr} BETWEEN ? AND ?")
                params.extend([_number(value[0], name), _number(value[1], name)])
            else:
                sql_op = "<>" if op == "!=" else op
                clauses.append(f"{expr} {sql_op} ?")
                params.append(_number(value, name))

        elif name in DAY_FIELDS:
            clauses.append(DAY_FIELDS[name])
            params.append(_number(value, name))

        elif name in BOOL_FIELDS:
            sql = BOOL_FIELDS[name]
            clauses.append(sql if value in (True, "true", 1, "1") else f"NOT ({sql})")

        else:
            raise _bad(f"unknown filter field {name!r}")

    return "(" + joiner.join(clauses) + ")", params


# --- sorting --------------------------------------------------------------

SORTABLE = {
    "title": ("COALESCE(NULLIF(t.title, ''), t.file_path)", "text"),
    "artist": ("t.artist", "text"),
    "album": ("t.album", "text"),
    "genre": ("t.genre", "text"),
    "format": ("t.format", "text"),
    "year": ("t.year", "num"),
    "duration": ("t.duration_seconds", "num"),
    "rating": ("t.rating", "num"),
    "bitrate": ("t.bitrate", "num"),
    "added": ("t.date_added", "text"),
    "plays": ("COALESCE(h.plays, 0)", "num"),
    "last_played": ("h.last_played", "num"),
    "track": ("t.track_number", "num"),
    "favorite": ("t.favorite", "num"),
}

# Within equal primary keys keep albums together and in disc/track order.
ORDER_TAIL = (
    "LOWER(COALESCE(NULLIF(t.album_artist, ''), t.artist, '')), LOWER(COALESCE(t.album, '')), "
    "COALESCE(t.disc_number, 0), COALESCE(t.track_number, 0), t.id"
)


def order_clause(sort: str, direction: str) -> str:
    expr, kind = SORTABLE.get(sort, SORTABLE["artist"])
    d = "DESC" if direction == "desc" else "ASC"
    if kind == "text":
        parts = [f"CASE WHEN {expr} IS NULL OR {expr} = '' THEN 1 ELSE 0 END", f"LOWER({expr}) {d}"]
    else:
        parts = [f"CASE WHEN {expr} IS NULL THEN 1 ELSE 0 END", f"{expr} {d}"]
    return ", ".join(parts + [ORDER_TAIL])


# --- album keys -----------------------------------------------------------


def album_key(album_artist: str, album: str) -> str:
    raw = json.dumps([album_artist, album], ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_album_key(key: str) -> tuple[str, str]:
    try:
        padded = key + "=" * (-len(key) % 4)
        album_artist, album = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return str(album_artist), str(album)
    except Exception:
        raise HTTPException(status_code=404, detail="unknown album") from None


# --- selections -----------------------------------------------------------


@dataclass
class Selection:
    """A FROM/WHERE/ORDER over tracks, reusable for listing, counting, ids."""

    joins: str = ""
    where: list[str] = field(default_factory=lambda: ["t.is_missing = 0"])
    params: list[Any] = field(default_factory=list)
    order: str = ORDER_TAIL
    playlist_position: bool = False

    @property
    def from_sql(self) -> str:
        return f"FROM tracks t {HISTORY_JOIN} {self.joins} WHERE {' AND '.join(self.where)}"


def parse_rules(raw: str | dict | None) -> dict[str, Any] | None:
    if raw in (None, "", {}):
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise _bad("rules must be valid JSON") from None
    if not isinstance(parsed, dict):
        raise _bad("rules must be a JSON object")
    return parsed


def build_selection(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    rules: dict[str, Any] | None = None,
    playlist_id: int | None = None,
    album: str | None = None,
    artist: str | None = None,
    sort: str = "artist",
    direction: str = "asc",
) -> Selection:
    sel = Selection()
    sel.order = order_clause(sort, direction)

    if q:
        for token in q.lower().split():
            sel.where.append(f"{SEARCH_HAYSTACK} LIKE ? ESCAPE '\\'")
            sel.params.append(_like(token, "contains"))

    if album:
        album_artist, album_name = parse_album_key(album)
        sel.where.append(f"{ALBUM_ARTIST_FIND} = ? AND {ALBUM_FIND} = ?")
        sel.params.extend([album_artist, album_name])
        if sort == "artist":  # inside one album the natural order is disc/track
            sel.order = "COALESCE(t.disc_number, 0), COALESCE(t.track_number, 0), t.id"

    if artist:
        sel.where.append(f"({ARTIST_SQL} = ? OR {ALBUM_ARTIST_SQL} = ?)")
        sel.params.extend([artist, artist])

    if playlist_id is not None:
        row = conn.execute("SELECT kind, rules_json FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        if row["kind"] == "smart":
            smart_sql, smart_params = build_rule_sql(parse_rules(row["rules_json"]))
            if smart_sql:
                sel.where.append(smart_sql)
                sel.params.extend(smart_params)
        else:
            sel.joins += " JOIN playlist_tracks pt ON pt.track_id = t.id AND pt.playlist_id = ?"
            sel.params.insert(0, playlist_id)  # the JOIN's placeholder comes before the WHERE's
            sel.playlist_position = True
            if sort == "position":  # the playlist's own order (what you get unless you sort by a column)
                sel.order = "pt.position, pt.rowid"

    rule_sql, rule_params = build_rule_sql(rules)
    if rule_sql:
        sel.where.append(rule_sql)
        sel.params.extend(rule_params)
    return sel


def track_dict(row: sqlite3.Row) -> dict[str, Any]:
    path = row["file_path"]
    artist = row["artist"] or row["album_artist"] or UNKNOWN_ARTIST
    album_artist = row["album_artist"] or row["artist"] or UNKNOWN_ARTIST
    album = row["album"] or UNKNOWN_ALBUM
    keys = row.keys()
    data = {
        "id": row["id"],
        "title": row["title"] or Path(path).stem,
        "artist": artist,
        "album_artist": album_artist,
        "album": album,
        "track": row["track_number"],
        "disc": row["disc_number"],
        "year": row["year"],
        "genre": row["genre"],
        "duration": row["duration_seconds"] or 0,
        "rating": row["rating"] or 0,
        "favorite": bool(row["favorite"]),
        "format": row["format"],
        "bitrate": row["bitrate"],
        "added": row["date_added"],
        "plays": row["plays"],
        "last_played": row["last_played"],
        "album_key": album_key(album_artist, album),
    }
    if "pos" in keys:
        data["pos"] = row["pos"]
    return data


def page_tracks(conn: sqlite3.Connection, sel: Selection, offset: int, limit: int) -> list[dict[str, Any]]:
    """One page of a selection, without counting how many songs match (a shelf has no use for that)."""
    extra = ", pt.position AS pos" if sel.playlist_position else ""
    rows = conn.execute(
        f"SELECT {TRACK_COLUMNS}{extra} {sel.from_sql} ORDER BY {sel.order} LIMIT ? OFFSET ?",
        [*sel.params, limit, offset],
    ).fetchall()
    return [track_dict(r) for r in rows]


def list_tracks(conn: sqlite3.Connection, sel: Selection, offset: int, limit: int) -> dict[str, Any]:
    total = conn.execute(f"SELECT COUNT(*) AS c {sel.from_sql}", sel.params).fetchone()["c"]
    return {"total": total, "items": page_tracks(conn, sel, offset, limit)}


def tracks_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[dict[str, Any]]:
    """Tracks in exactly the requested order (the queue is order-sensitive); unknown ids are skipped."""
    if not ids:
        return []
    placeholders = ", ".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT {TRACK_COLUMNS} FROM tracks t {HISTORY_JOIN} WHERE t.is_missing = 0 AND t.id IN ({placeholders})",
        ids,
    ).fetchall()
    by_id = {r["id"]: track_dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


# --- shelves: the first few of a very large listing, without ordering all of it ----------------------------------------

PLAYED_FIGURES = {"plays": "h.plays", "last_played": "h.last_played"}


def played_leaders(conn: sqlite3.Connection, by: str, limit: int) -> list[dict[str, Any]]:
    """The songs played most (by="plays") or most recently (by="last_played"), in the order the library lists them.

    track_plays holds one row per song that has been played (a song with no plays has none), with an index on each
    figure, so the figure of the song that just makes the cut is found by walking an index, and only the songs at or
    above it are put in order: not every song ever played."""
    column = PLAYED_FIGURES[by]
    limit = max(1, limit)
    start = "FROM track_plays h CROSS JOIN tracks t ON t.id = h.track_id WHERE t.is_missing = 0"
    cut = conn.execute(f"SELECT {column} {start} ORDER BY {column} DESC LIMIT 1 OFFSET ?", (limit - 1,)).fetchone()
    reach, params = (f" AND {column} >= ?", [cut[0]]) if cut is not None else ("", [])
    rows = conn.execute(
        f"SELECT {TRACK_COLUMNS} {start}{reach} ORDER BY {order_clause(by, 'desc')} LIMIT ?", [*params, limit]
    ).fetchall()
    return [track_dict(r) for r in rows]


ALBUM_FIGURES = "MIN(t.year) AS year, COUNT(*) AS n, SUM(t.duration_seconds) AS dur, MAX(t.date_added) AS added, MIN(t.id) AS first_id"


def albums_by_key(conn: sqlite3.Connection, keys: list[tuple[str, str]]) -> list[sqlite3.Row]:
    """Each named album's figures (the library's own grouping), in the order asked; one the library lacks is left out.
    The songs of one album are found through idx_tracks_album_key, so this costs the same in any size of library."""
    found = []
    for artist, album in keys:
        row = conn.execute(
            f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al, {ALBUM_FIGURES} FROM tracks t "
            f"WHERE t.is_missing = 0 AND {ALBUM_ARTIST_FIND} = ? AND {ALBUM_FIND} = ?",
            (artist, album),
        ).fetchone()
        if row["n"]:
            found.append(row)
    return found


def newest_albums(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """The albums whose newest song was added most recently, newest first.

    Songs are read newest first (idx_tracks_added) until `limit` different albums have turned up; grouping the whole
    library to find them would take longer than every other part of the Home screen together."""
    window = max(40, limit * 8)
    while True:
        rows = conn.execute(
            f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al FROM tracks t WHERE t.is_missing = 0 "
            f"ORDER BY t.date_added DESC, t.id DESC LIMIT ?",
            (window,),
        ).fetchall()
        keys = list(dict.fromkeys((r["aa"], r["al"]) for r in rows))
        if len(keys) >= limit or len(rows) < window:  # enough albums, or the whole library has been read
            return albums_by_key(conn, keys[:limit])
        window *= 4


SMALL_LIBRARY = 5000  # songs: up to this many, grouping everything to pick albums at random is quick
MAX_PROBES = 600


def random_albums(conn: sqlite3.Connection, limit: int, songs: int) -> list[sqlite3.Row]:
    """Albums chosen at random, each album as likely as any other whatever its length. `songs` is how many songs
    the library has.

    A big library is not grouped: a random song is picked (a lookup by number), its album's figures are read, and the
    album is kept with probability 1/(its songs), which makes every album equally likely. If that does not turn up
    enough albums (a library of few, very long albums), the whole library is grouped as a small one is."""
    if songs > SMALL_LIBRARY:
        low = conn.execute("SELECT MIN(id) FROM tracks WHERE is_missing = 0").fetchone()[0]
        high = conn.execute("SELECT MAX(id) FROM tracks WHERE is_missing = 0").fetchone()[0]
        chosen: dict[tuple[str, str], sqlite3.Row] = {}
        for _ in range(MAX_PROBES):
            row = conn.execute(
                f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al FROM tracks t "
                f"WHERE t.is_missing = 0 AND t.id >= ? ORDER BY t.id LIMIT 1",
                (random.randint(low, high),),
            ).fetchone()
            key = (row["aa"], row["al"]) if row is not None else None
            if key is None or key in chosen:
                continue
            found = albums_by_key(conn, [key])
            if found and random.random() * found[0]["n"] < 1:
                chosen[key] = found[0]
                if len(chosen) == limit:
                    return list(chosen.values())
    inner = (
        f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al, t.year AS year, t.duration_seconds AS dur, "
        f"t.date_added AS added, t.id AS id FROM tracks t WHERE t.is_missing = 0"
    )
    return conn.execute(
        f"SELECT aa, al, MIN(year) AS year, COUNT(*) AS n, SUM(dur) AS dur, MAX(added) AS added, MIN(id) AS first_id "
        f"FROM ({inner}) GROUP BY aa, al ORDER BY RANDOM() LIMIT ?",
        (limit,),
    ).fetchall()
