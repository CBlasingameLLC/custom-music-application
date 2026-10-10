"""Finding the same song twice, choosing the copy to keep, and parking the others in a review folder.

Nothing is deleted here. A removed copy is moved into `<library>/_duplicates_review/` (mirroring where it
lived), hidden from the library and skipped by scans, and what was attached to it (plays, playlist entries,
rating, favorite) is handed to the copy that stays. The whole thing can be undone, and only an explicit
purge, which goes to the Recycle Bin, ends a copy's life."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from musictoolkit.ingest import cleanup, moves
from musictoolkit.ingest.scanner import QUARANTINE_DIR, compute_content_hash, in_quarantine

logger = logging.getLogger("musictoolkit")

Progress = Callable[[int, int], None]

LOSSLESS = {"flac", "wav", "aiff", "ape", "wv", "alac"}
REASON_LABELS = {
    "content_hash": "Identical files",
    "musicbrainz_recording_id": "Same MusicBrainz recording",
    "normalized_artist_title_duration": "Same artist, title and length",
}
IGNORED_KEY = "dedupe.ignored"


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch.lower() for ch in value if ch.isalnum())


@dataclass
class DuplicateGroup:
    key: str
    reason: str
    track_ids: list[int]
    file_paths: list[str]
    identical: bool = False  # every copy is byte for byte the same file (only known after a content check)

    @property
    def ignore_key(self) -> str:
        """What "these are not duplicates" remembers. It names the match, not the files, so a later rescan
        does not bring the same pair back."""
        return f"{self.reason}:{self.key}"


# ------------------------------------------------------------------------------------------ finding


def _rows_in(conn: sqlite3.Connection, roots: Path | str | Iterable[Path | str] | None) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM tracks WHERE is_missing = 0").fetchall()
    if roots is None:
        return rows
    wanted = [Path(roots)] if isinstance(roots, (str, Path)) else [Path(r) for r in roots]
    resolved = [r.resolve() for r in wanted]
    return [r for r in rows if any(Path(r["file_path"]).is_relative_to(root) for root in resolved)]


def find_duplicate_groups(
    conn: sqlite3.Connection,
    library_root: Path | str | Iterable[Path | str] | None,
    use_content_hash: bool = False,
    on_progress: Progress | None = None,
    ignored: Iterable[str] = (),
) -> list[DuplicateGroup]:
    """Groups of songs that look like the same recording. `library_root` may be one folder, several, or None
    for the whole library. With `use_content_hash` files of equal size are also compared byte for byte, which
    finds exact copies whatever their tags say and marks groups that are fully identical."""
    skip = set(ignored)
    rows = _rows_in(conn, library_root)
    groups: list[DuplicateGroup] = []
    covered_ids: set[int] = set()

    def emit(key: str, reason: str, members: list[sqlite3.Row]) -> None:
        group = DuplicateGroup(key=key, reason=reason, track_ids=[r["id"] for r in members], file_paths=[r["file_path"] for r in members])
        covered_ids.update(group.track_ids)  # an ignored group still keeps its songs out of the later passes
        if group.ignore_key not in skip:
            groups.append(group)

    # Pass 1: exact MusicBrainz recording ID match - highest confidence.
    by_mbid: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        if row["musicbrainz_recording_id"]:
            by_mbid[row["musicbrainz_recording_id"]].append(row)
    for mbid, members in by_mbid.items():
        if len(members) > 1:
            emit(mbid, "musicbrainz_recording_id", members)

    # Pass 2: normalized artist+title+duration (2s bucket tolerance).
    by_signature: dict[tuple[str, str, int], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        if row["id"] in covered_ids:
            continue
        duration_bucket = round((row["duration_seconds"] or 0) / 2) * 2
        sig = (_normalize(row["artist"]), _normalize(row["title"]), duration_bucket)
        if sig[0] and sig[1]:
            by_signature[sig].append(row)
    for sig, members in by_signature.items():
        if len(members) > 1:
            emit(f"{sig[0]}::{sig[1]}", "normalized_artist_title_duration", members)

    # Pass 3 (opt-in, reads files): byte-identical content. Only files of the same size can be identical.
    if use_content_hash:
        by_size: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            if row["file_size"]:
                by_size[row["file_size"]].append(row)
        candidates = [r for members in by_size.values() if len(members) > 1 for r in members]
        hashes: dict[int, str | None] = {}
        for done, row in enumerate(candidates):
            if on_progress:
                on_progress(done, len(candidates))
            hashes[row["id"]] = _hash_of(conn, row)
        conn.commit()
        if on_progress:
            on_progress(len(candidates), len(candidates))

        by_hash: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in candidates:
            if row["id"] not in covered_ids and hashes.get(row["id"]):
                by_hash[hashes[row["id"]]].append(row)
        for file_hash, members in by_hash.items():
            if len(members) > 1:
                emit(file_hash, "content_hash", members)
        for group in groups:
            seen = {hashes.get(track_id) for track_id in group.track_ids}
            group.identical = None not in seen and len(seen) == 1
    return groups


def _hash_of(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """The file's content hash, remembered in the library so a second check does not read it again. A file
    that cannot be read has none (a scan notices the missing ones)."""
    if row["file_hash"]:
        return row["file_hash"]
    try:
        file_hash = compute_content_hash(Path(row["file_path"]))
    except OSError:
        return None
    conn.execute("UPDATE tracks SET file_hash = ? WHERE id = ?", (file_hash, row["id"]))
    return file_hash


# ------------------------------------------------------------------------------------------ choosing


def _rank(row: sqlite3.Row, plays: int) -> tuple:
    """Higher is better: lossless, then bitrate, then file size, then how completely tagged, then how often played."""
    filled = sum(1 for field in ("title", "artist", "album", "album_artist", "year", "genre", "track_number") if row[field])
    return ((row["format"] or "").lower() in LOSSLESS, row["bitrate"] or 0, row["file_size"] or 0, filled, plays, -row["id"])


def _play_counts(conn: sqlite3.Connection, ids: list[int]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for chunk in _chunked(ids):
        for row in conn.execute(
            f"SELECT track_id, COUNT(*) AS n FROM play_history WHERE track_id IN ({','.join('?' * len(chunk))}) GROUP BY track_id", chunk
        ):
            counts[row["track_id"]] = row["n"]
    return counts


def _chunked(ids: list[int], size: int = 500):
    for start in range(0, len(ids), size):
        yield ids[start:start + size]


def _pick_keeper(conn: sqlite3.Connection, track_ids: list[int]) -> int:
    """The best copy to keep (see `_rank`); ties go to the one added first."""
    rows = []
    for chunk in _chunked(track_ids):
        rows += conn.execute(f"SELECT * FROM tracks WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    plays = _play_counts(conn, track_ids)
    return max(rows, key=lambda r: _rank(r, plays.get(r["id"], 0)))["id"]


def describe_groups(conn: sqlite3.Connection, groups: list[DuplicateGroup]) -> list[dict[str, Any]]:
    """What the review screen shows: every copy with the facts that decide which to keep, the best one marked.
    Groups that would free the most space come first."""
    ids = [track_id for group in groups for track_id in group.track_ids]
    rows: dict[int, sqlite3.Row] = {}
    for chunk in _chunked(ids):
        for row in conn.execute(f"SELECT * FROM tracks WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            rows[row["id"]] = row
    plays = _play_counts(conn, ids)
    described = []
    for group in groups:
        members = [rows[i] for i in group.track_ids if i in rows]
        if len(members) < 2:
            continue
        members.sort(key=lambda r: _rank(r, plays.get(r["id"], 0)), reverse=True)
        keeper = members[0]["id"]
        wasted = sum(r["file_size"] or 0 for r in members[1:])
        described.append({
            "key": group.ignore_key,
            "reason": group.reason,
            "label": "Identical files" if group.identical else REASON_LABELS[group.reason],
            "identical": group.identical,
            "keeper": keeper,
            "wasted": wasted,
            "members": [
                {
                    "id": r["id"], "path": r["file_path"], "title": r["title"], "artist": r["artist"], "album": r["album"],
                    "year": r["year"], "format": r["format"], "bitrate": r["bitrate"], "size": r["file_size"],
                    "duration": r["duration_seconds"], "plays": plays.get(r["id"], 0), "rating": r["rating"] or 0,
                    "favorite": bool(r["favorite"]), "added": r["date_added"],
                }
                for r in members
            ],
        })
    described.sort(key=lambda g: (-g["wasted"], g["key"]))
    return described


# "These are not duplicates": remembered by match key in the library's small key/value store.


def load_ignored(conn: sqlite3.Connection) -> set[str]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (IGNORED_KEY,)).fetchone()
    try:
        return set(json.loads(row["value"])) if row else set()
    except (ValueError, TypeError):
        return set()


def _save_ignored(conn: sqlite3.Connection, keys: set[str]) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (IGNORED_KEY, json.dumps(sorted(keys))),
    )
    conn.commit()


def ignore_groups(conn: sqlite3.Connection, keys: Iterable[str]) -> int:
    known = load_ignored(conn)
    added = set(keys) - known
    _save_ignored(conn, known | added)
    return len(added)


def reset_ignored(conn: sqlite3.Connection) -> int:
    known = load_ignored(conn)
    _save_ignored(conn, set())
    return len(known)


# ------------------------------------------------------------------------------------------ merging


def merge_into(conn: sqlite3.Connection, keeper_id: int, dup_id: int) -> dict[str, Any] | None:
    """Hand a removed copy's plays, playlist entries, rating and favorite to the copy that stays. Returns the
    undo record (what exactly moved), or None when there was nothing to carry over. The caller commits."""
    record: dict[str, Any] = {"kind": "merge", "keeper": keeper_id, "plays": [], "playlist_moved": [], "playlist_dropped": []}

    record["plays"] = [r["id"] for r in conn.execute("SELECT id FROM play_history WHERE track_id = ?", (dup_id,))]
    if record["plays"]:
        conn.execute("UPDATE play_history SET track_id = ? WHERE track_id = ?", (keeper_id, dup_id))

    for entry in conn.execute("SELECT rowid AS rid, playlist_id, position FROM playlist_tracks WHERE track_id = ?", (dup_id,)).fetchall():
        already = conn.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (entry["playlist_id"], keeper_id)
        ).fetchone()
        if already:  # the playlist has the kept copy too: one entry is enough
            conn.execute("DELETE FROM playlist_tracks WHERE rowid = ?", (entry["rid"],))
            record["playlist_dropped"].append({"playlist_id": entry["playlist_id"], "position": entry["position"]})
        else:
            conn.execute("UPDATE playlist_tracks SET track_id = ? WHERE rowid = ?", (keeper_id, entry["rid"]))
            record["playlist_moved"].append(entry["rid"])

    keeper = conn.execute("SELECT rating, favorite FROM tracks WHERE id = ?", (keeper_id,)).fetchone()
    dup = conn.execute("SELECT rating, favorite FROM tracks WHERE id = ?", (dup_id,)).fetchone()
    if keeper and dup:
        if (dup["rating"] or 0) > (keeper["rating"] or 0):
            record["rating"] = {"before": keeper["rating"], "after": dup["rating"]}
            conn.execute("UPDATE tracks SET rating = ? WHERE id = ?", (dup["rating"], keeper_id))
        if dup["favorite"] and not keeper["favorite"]:
            record["favorite"] = {"before": keeper["favorite"], "after": 1}
            conn.execute("UPDATE tracks SET favorite = 1 WHERE id = ?", (keeper_id,))

    nothing = not (record["plays"] or record["playlist_moved"] or record["playlist_dropped"] or "rating" in record or "favorite" in record)
    return None if nothing else record


# ------------------------------------------------------------------------------------------ review folder


def review_path(root: Path, old: Path) -> Path:
    """Where a removed copy goes: the same relative place under `<library>/_duplicates_review`, so the review
    folder reads like a copy of the library and two files of one name never meet."""
    target = root / QUARANTINE_DIR / old.relative_to(root)
    counter = 2
    while os.path.lexists(target):
        target = target.with_name(f"{old.stem} ({counter}){old.suffix}")
        counter += 1
    return target


def quarantine(
    conn: sqlite3.Connection,
    choices: list[tuple[int, list[int]]],
    roots: list[Path],
    on_progress: Progress | None = None,
) -> moves.BatchResult:
    """Move each `remove` copy into its library folder's review folder, carrying its plays and playlist entries
    over to `keeper`. A copy is only moved while the kept file is really there, so the last good copy of a song
    can never be put away. One undoable batch. If `on_progress` raises (a cancelled job) what moved so far
    stays logged."""
    result = moves.BatchResult(batch_id=uuid.uuid4().hex[:12])
    movers: dict[str, moves.Mover] = {}
    total = sum(len(remove) for _, remove in choices)
    done = 0
    try:
        for keeper_id, remove_ids in choices:
            keeper = conn.execute("SELECT file_path FROM tracks WHERE id = ? AND is_missing = 0", (keeper_id,)).fetchone()
            keeper_there = keeper is not None and Path(keeper["file_path"]).is_file()
            for dup_id in remove_ids:
                if on_progress:
                    on_progress(done, total)
                done += 1
                row = conn.execute("SELECT file_path FROM tracks WHERE id = ? AND is_missing = 0", (dup_id,)).fetchone()
                if row is None or dup_id == keeper_id:
                    _skip(result, row["file_path"] if row else str(dup_id), "That song is not in the library any more")
                    continue
                old = Path(row["file_path"])
                if not keeper_there:
                    _skip(result, old, "The copy chosen to stay is missing, so this one was left where it is")
                    continue
                root = moves.root_of(old, roots)
                if root is None:
                    _skip(result, old, "This song is not inside one of your library folders")
                    continue
                mover = movers.get(str(root))
                if mover is None:
                    mover = movers[str(root)] = moves.Mover(conn, "quarantine", root, copy_art=True, hide=True, batch_id=result.batch_id)
                mover.move(dup_id, old, review_path(root, old), extra=lambda c, k=keeper_id, d=dup_id: merge_into(c, k, d))
        if on_progress:
            on_progress(total, total)
    finally:
        for mover in movers.values():
            result.absorb(mover.finish())
    return result


def _skip(result: moves.BatchResult, path: Path | str, reason: str) -> None:
    result.skipped += 1
    result.errors.append({"path": str(path), "error": reason})


def apply_quarantine(conn: sqlite3.Connection, groups: list[DuplicateGroup], library_root: Path) -> int:
    """Command-line entry point: keep each group's best copy, quarantine the rest. Returns how many moved."""
    choices = []
    for group in groups:
        keeper = _pick_keeper(conn, group.track_ids)
        choices.append((keeper, [i for i in group.track_ids if i != keeper]))
    return quarantine(conn, choices, [library_root.resolve()]).moved


# ------------------------------------------------------------------------------------------ the review folder


def quarantined_summary(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(file_size), 0) AS size FROM tracks WHERE is_missing = 2").fetchone()
    return {"count": row["n"], "bytes": row["size"]}


def list_quarantined(conn: sqlite3.Connection, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM tracks WHERE is_missing = 2 ORDER BY file_path LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM tracks WHERE is_missing = 2").fetchone()[0]
    items = []
    for row in rows:
        logged = conn.execute(
            "SELECT old_path, moved_at FROM file_moves WHERE kind = 'quarantine' AND track_id = ? AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        home = Path(logged["old_path"]) if logged else moves.home_of_quarantined(Path(row["file_path"]))
        items.append({
            "id": row["id"], "path": row["file_path"], "original": str(home) if home else None,
            "title": row["title"], "artist": row["artist"], "album": row["album"], "format": row["format"],
            "bitrate": row["bitrate"], "size": row["file_size"], "moved_at": logged["moved_at"] if logged else None,
            "exists": Path(row["file_path"]).is_file(),
        })
    return {"total": total, "items": items}


def quarantined_ids(conn: sqlite3.Connection) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM tracks WHERE is_missing = 2")]


def restore(conn: sqlite3.Connection, track_ids: list[int], roots: list[Path], on_progress: Progress | None = None) -> moves.BatchResult:
    """Bring copies back from the review folder to where they were, with their lyrics, and give them back
    what was carried over to the copy that stayed."""
    return moves.undo_tracks(conn, track_ids, "quarantine", roots, on_progress)


def purge(
    conn: sqlite3.Connection,
    track_ids: list[int],
    roots: list[Path],
    on_progress: Progress | None = None,
    trash: Callable[[str], None] | None = None,
) -> moves.BatchResult:
    """Send copies in the review folder to the Recycle Bin and forget them. This is the one step that cannot
    be undone from here (the Recycle Bin is the way back). It only ever touches files inside a review folder."""
    if trash is None:
        from send2trash import send2trash as trash  # noqa: PLC0415 - only needed here, and keeps start-up light
    result = moves.BatchResult(batch_id="purge")
    ids = list(dict.fromkeys(track_ids))
    folders: set[Path] = set()
    forgotten: list[int] = []
    try:
        for done, track_id in enumerate(ids):
            if on_progress:
                on_progress(done, len(ids))
            row = conn.execute("SELECT id, file_path FROM tracks WHERE id = ? AND is_missing = 2", (track_id,)).fetchone()
            if row is None:
                _skip(result, str(track_id), "That song is not in the review folder")
                continue
            path = Path(row["file_path"])
            root = moves.root_of(path, roots)
            if root is None or not in_quarantine(path, root):
                _skip(result, path, "Only files inside a review folder can be deleted here")
                continue
            try:
                if path.exists():
                    trash(str(path))
            except Exception as exc:  # send2trash raises OSError subclasses, but a COM failure can be anything
                logger.warning("Could not move %s to the Recycle Bin: %s", path, exc)
                _skip(result, path, f"Could not move it to the Recycle Bin ({exc})")
                continue
            for sidecar in moves.sidecars(path):  # after the song is gone, so a shared lyrics file goes with its last owner
                try:
                    trash(str(sidecar))
                except Exception as exc:
                    logger.warning("Could not move %s to the Recycle Bin: %s", sidecar, exc)
            folders.add(path.parent)
            forgotten.append(track_id)
            result.moved += 1
            if len(forgotten) >= 200:  # keep each database transaction short
                cleanup.forget_tracks(conn, forgotten)
                forgotten = []
        if on_progress:
            on_progress(len(ids), len(ids))
    finally:
        if forgotten:
            cleanup.forget_tracks(conn, forgotten)
        for folder in sorted(folders, key=lambda d: len(d.parts), reverse=True):
            root = moves.root_of(folder, roots)
            if root is not None:
                result.tidied_folders += moves.prune_empty_folders(folder, root, allow_art=True)
    return result
