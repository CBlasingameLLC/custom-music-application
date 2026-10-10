from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from musictoolkit.ingest import tagedit, tagwrite
from musictoolkit.integrations import musicbrainz_client

logger = logging.getLogger("musictoolkit")

# Tracks already attempted (matched, no match, or turned down) carry one of these in tag_source
# and are skipped on re-run — this is what makes enrichment resumable/safe
# to interrupt against MusicBrainz's ~1 req/sec rate limit.
_ALREADY_ATTEMPTED = ("musicbrainz", "musicbrainz_no_match", "musicbrainz_rejected")

FILLABLE = ("title", "artist", "album")  # the tags a MusicBrainz match can supply

Progress = Callable[[int, int], None]


@dataclass
class TagProposal:
    track_id: int
    file_path: str
    proposed: dict
    mb_confidence: float


@dataclass
class TagResult:
    proposals: list[TagProposal]
    skipped_no_match: int = 0
    errors: int = 0


def find_sparse_tracks(conn: sqlite3.Connection, root: Path | None = None) -> list[sqlite3.Row]:
    """Tracks missing a title, artist or album that nobody has dealt with yet.

    Tracks with a proposal waiting for review (or already decided) are left out too, so a
    second lookup never repeats work. `root=None` means the whole library."""
    placeholders = ",".join("?" * len(_ALREADY_ATTEMPTED))
    rows = conn.execute(
        f"""
        SELECT * FROM tracks
        WHERE is_missing = 0
          AND (tag_source IS NULL OR tag_source NOT IN ({placeholders}))
          AND (title IS NULL OR artist IS NULL OR album IS NULL)
          AND id NOT IN (SELECT track_id FROM tag_proposals)
        ORDER BY id
        """,
        _ALREADY_ATTEMPTED,
    ).fetchall()
    if root is None:
        return rows
    root = root.resolve()
    return [r for r in rows if Path(r["file_path"]).is_relative_to(root)]


_TRACK_NUMBER_PREFIX = re.compile(r"^\s*(?:disc\s*\d+\s*)?\(?\d{1,3}\)?\s*[-._)]?\s+", re.IGNORECASE)


def _guess_from_filename(path: Path) -> tuple[str | None, str | None]:
    """Best-effort 'Artist - Title' filename guess, used only as an MB search seed.

    A leading track number ("01 - ", "07. ", "(3) ") is not an artist, so it is dropped first."""
    stem = _TRACK_NUMBER_PREFIX.sub("", path.stem).strip() or path.stem.strip()
    if " - " in stem:
        artist, _, title = stem.partition(" - ")
        return artist.strip(), title.strip()
    return None, stem


def _proposal_from_match(match: dict, seed_artist: str | None, seed_title: str | None) -> tuple[dict, float]:
    confidence = int(match.get("ext:score", 0)) / 100.0
    proposed = {
        "title": match.get("title") or seed_title,
        "artist": match.get("artist-credit-phrase") or seed_artist,
        "musicbrainz_recording_id": match.get("id"),
    }
    release_list = match.get("release-list") or []
    if release_list:
        proposed["album"] = release_list[0].get("title")
        proposed["musicbrainz_release_id"] = release_list[0].get("id")
    return proposed, confidence


def propose_tags(
    conn: sqlite3.Connection,
    root: Path | None = None,
    on_progress: Progress | None = None,
    limit: int | None = None,
    persist: bool = False,
) -> TagResult:
    """Query MusicBrainz for every sparse track under root. Always persists a
    'no match found' bookkeeping mark immediately (regardless of --apply) so
    a rate-limited re-run doesn't re-query the same dead ends.

    A *successful* match is returned to the caller and, with persist=True (the desktop app), saved in
    tag_proposals so it survives a restart until a person approves or dismisses it. It never touches
    tracks.tag_source: only applying (or dismissing) a proposal marks a track as done, otherwise a
    dry-run would silently consume the proposal before it was ever actually written."""
    result = TagResult(proposals=[])
    rows = find_sparse_tracks(conn, root)
    if limit is not None:
        rows = rows[:limit]
    now = datetime.now(timezone.utc).isoformat()

    for done, row in enumerate(rows):
        if on_progress:
            on_progress(done, len(rows))
        guessed_artist, guessed_title = _guess_from_filename(Path(row["file_path"]))
        seed_artist = row["artist"] or guessed_artist
        seed_title = row["title"] or guessed_title

        if not seed_title:
            result.skipped_no_match += 1
            continue

        try:
            match = musicbrainz_client.best_match(seed_artist or "", seed_title)
        except Exception:
            logger.warning("MusicBrainz lookup failed for %s", row["file_path"], exc_info=True)
            result.errors += 1
            continue

        if match is None:
            result.skipped_no_match += 1
            conn.execute("UPDATE tracks SET tag_source = 'musicbrainz_no_match' WHERE id = ?", (row["id"],))
            conn.commit()
            continue

        proposed, confidence = _proposal_from_match(match, seed_artist, seed_title)
        result.proposals.append(
            TagProposal(track_id=row["id"], file_path=row["file_path"], proposed=proposed, mb_confidence=confidence)
        )
        if persist:
            conn.execute(
                "INSERT OR REPLACE INTO tag_proposals (track_id, proposed_json, confidence, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (row["id"], json.dumps(proposed), confidence, now),
            )
            conn.commit()

    if on_progress:
        on_progress(len(rows), len(rows))
    conn.commit()
    return result


def apply_tags(conn: sqlite3.Connection, proposals: list[TagProposal]) -> None:
    """Command-line behaviour: write every proposed title/artist/album into its file, replacing what is there."""
    now = datetime.now(timezone.utc).isoformat()
    for p in proposals:
        path = Path(p.file_path)
        changes = {key: p.proposed[key] for key in FILLABLE if p.proposed.get(key)}
        try:
            tagwrite.apply_changes(path, tagwrite.clean_changes(changes))
        except tagwrite.TagWriteError as exc:
            logger.warning("Could not write tags to %s: %s", path, exc)
            continue
        logger.info("Wrote MusicBrainz tags to %s (confidence=%.2f)", path, p.mb_confidence)

        conn.execute(
            """
            UPDATE tracks SET
                title = COALESCE(?, title),
                artist = COALESCE(?, artist),
                album = COALESCE(?, album),
                musicbrainz_recording_id = ?,
                musicbrainz_release_id = ?,
                mb_match_confidence = ?,
                tag_source = 'musicbrainz',
                date_last_scanned = ?
            WHERE id = ?
            """,
            (
                p.proposed.get("title"), p.proposed.get("artist"), p.proposed.get("album"),
                p.proposed.get("musicbrainz_recording_id"), p.proposed.get("musicbrainz_release_id"),
                p.mb_confidence, now, p.track_id,
            ),
        )
    conn.commit()


# ----------------------------------------------------------------------------- review queue (desktop app)


def summary(conn: sqlite3.Connection) -> dict[str, int]:
    def one(sql: str, *args: Any) -> int:
        return conn.execute(sql, args).fetchone()[0]

    status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM tag_proposals GROUP BY status")}
    return {
        "to_look_up": len(find_sparse_tracks(conn)),
        "pending": status.get("pending", 0),
        "applied": status.get("applied", 0),
        "dismissed": status.get("dismissed", 0),
        "no_match": one("SELECT COUNT(*) FROM tracks WHERE is_missing = 0 AND tag_source = 'musicbrainz_no_match'"),
    }


def _changes_for(track: sqlite3.Row, proposed: dict, overwrite: bool) -> dict[str, str]:
    """What applying this proposal would write: empty tags are filled; filled ones only with overwrite=True."""
    changes: dict[str, str] = {}
    for key in FILLABLE:
        wanted = proposed.get(key)
        current = track[key]
        if wanted and wanted != current and (overwrite or not current):
            changes[key] = wanted
    return changes


def list_proposals(
    conn: sqlite3.Connection, offset: int = 0, limit: int = 50, min_confidence: float = 0.0, overwrite: bool = False
) -> dict[str, Any]:
    where = "p.status = 'pending' AND t.is_missing = 0 AND p.confidence >= ?"
    total = conn.execute(
        f"SELECT COUNT(*) FROM tag_proposals p JOIN tracks t ON t.id = p.track_id WHERE {where}", (min_confidence,)
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT p.proposed_json, p.confidence, t.* FROM tag_proposals p JOIN tracks t ON t.id = p.track_id "
        f"WHERE {where} ORDER BY p.confidence DESC, t.id LIMIT ? OFFSET ?",
        (min_confidence, limit, offset),
    ).fetchall()
    items = []
    for r in rows:
        proposed = json.loads(r["proposed_json"])
        items.append(
            {
                "track_id": r["id"],
                "file": Path(r["file_path"]).name,
                "path": r["file_path"],
                "confidence": r["confidence"],
                "current": {key: r[key] for key in FILLABLE},
                "proposed": {key: proposed.get(key) for key in FILLABLE},
                "will_write": _changes_for(r, proposed, overwrite),
            }
        )
    return {"total": total, "items": items}


def _selected(conn: sqlite3.Connection, track_ids: list[int] | None, min_confidence: float | None) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT p.proposed_json, p.confidence, t.* FROM tag_proposals p JOIN tracks t ON t.id = p.track_id "
        "WHERE p.status = 'pending' AND t.is_missing = 0 ORDER BY p.confidence DESC, t.id"
    ).fetchall()
    wanted = set(track_ids) if track_ids is not None else None
    return [
        r
        for r in rows
        if (wanted is None or r["id"] in wanted) and (min_confidence is None or r["confidence"] >= min_confidence)
    ]


def apply_proposals(
    conn: sqlite3.Connection,
    track_ids: list[int] | None = None,
    min_confidence: float | None = None,
    overwrite: bool = False,
    on_progress: Progress | None = None,
) -> tagedit.EditResult:
    """Write approved proposals into the files as one undoable batch, then mark them done."""
    rows = _selected(conn, track_ids, min_confidence)
    plan = [(r["id"], _changes_for(r, json.loads(r["proposed_json"]), overwrite)) for r in rows]
    result = tagedit.edit_each(conn, plan, on_progress, tag_source="musicbrainz")

    now = datetime.now(timezone.utc).isoformat()
    done_ids = set(result.edited_ids) | set(result.unchanged_ids)
    for r in rows:
        if r["id"] not in done_ids:
            continue  # the file could not be written; the proposal stays for another try
        proposed = json.loads(r["proposed_json"])
        conn.execute(
            "UPDATE tracks SET musicbrainz_recording_id = ?, musicbrainz_release_id = ?, mb_match_confidence = ?, "
            "tag_source = 'musicbrainz' WHERE id = ?",
            (proposed.get("musicbrainz_recording_id"), proposed.get("musicbrainz_release_id"), r["confidence"], r["id"]),
        )
        conn.execute("UPDATE tag_proposals SET status = 'applied', resolved_at = ? WHERE track_id = ?", (now, r["id"]))
    conn.commit()
    return result


def dismiss_proposals(conn: sqlite3.Connection, track_ids: list[int] | None = None, max_confidence: float | None = None) -> int:
    """Turn proposals down: the tracks keep their tags and are not looked up again."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        r
        for r in _selected(conn, track_ids, None)
        if max_confidence is None or r["confidence"] <= max_confidence
    ]
    for r in rows:
        conn.execute("UPDATE tag_proposals SET status = 'dismissed', resolved_at = ? WHERE track_id = ?", (now, r["id"]))
        conn.execute("UPDATE tracks SET tag_source = 'musicbrainz_rejected' WHERE id = ?", (r["id"],))
    conn.commit()
    return len(rows)


def forget_dead_ends(conn: sqlite3.Connection) -> int:
    """Let tracks with no match (or dismissed matches) be looked up again, e.g. after fixing their file names."""
    conn.execute("DELETE FROM tag_proposals WHERE status = 'dismissed'")
    cursor = conn.execute(
        "UPDATE tracks SET tag_source = 'original' WHERE tag_source IN ('musicbrainz_no_match', 'musicbrainz_rejected')"
    )
    conn.commit()
    return cursor.rowcount
