from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

from musictoolkit.integrations import musicbrainz_client

logger = logging.getLogger("musictoolkit")

# Tracks already attempted (matched or not) carry one of these in tag_source
# and are skipped on re-run — this is what makes enrichment resumable/safe
# to interrupt against MusicBrainz's ~1 req/sec rate limit.
_ALREADY_ATTEMPTED = ("musicbrainz", "musicbrainz_no_match")


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


def find_sparse_tracks(conn: sqlite3.Connection, root: Path) -> list[sqlite3.Row]:
    root = root.resolve()
    placeholders = ",".join("?" * len(_ALREADY_ATTEMPTED))
    rows = conn.execute(
        f"""
        SELECT * FROM tracks
        WHERE is_missing = 0
          AND (tag_source IS NULL OR tag_source NOT IN ({placeholders}))
          AND (title IS NULL OR artist IS NULL OR album IS NULL)
        """,
        _ALREADY_ATTEMPTED,
    ).fetchall()
    return [r for r in rows if Path(r["file_path"]).is_relative_to(root)]


def _guess_from_filename(path: Path) -> tuple[str | None, str | None]:
    """Best-effort 'Artist - Title' filename guess, used only as an MB search seed."""
    stem = path.stem
    if " - " in stem:
        artist, _, title = stem.partition(" - ")
        return artist.strip(), title.strip()
    return None, stem.strip()


def propose_tags(conn: sqlite3.Connection, root: Path) -> TagResult:
    """Query MusicBrainz for every sparse track under root. Always persists a
    'no match found' bookkeeping mark immediately (regardless of --apply) so
    a rate-limited re-run doesn't re-query the same dead ends. A *successful*
    match is intentionally NOT persisted here — only apply_tags() marks a
    track as done, otherwise a dry-run would silently consume the proposal
    before it was ever actually written."""
    result = TagResult(proposals=[])
    for row in find_sparse_tracks(conn, root):
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
            continue

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

        result.proposals.append(
            TagProposal(track_id=row["id"], file_path=row["file_path"], proposed=proposed, mb_confidence=confidence)
        )

    conn.commit()
    return result


def apply_tags(conn: sqlite3.Connection, proposals: list[TagProposal]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for p in proposals:
        path = Path(p.file_path)
        try:
            audio = EasyID3(path)
        except ID3NoHeaderError:
            audio = EasyID3()
            audio.save(path)
            audio = EasyID3(path)
        except Exception:
            logger.warning("Failed to open %s for tag writing", path, exc_info=True)
            continue

        if p.proposed.get("title"):
            audio["title"] = p.proposed["title"]
        if p.proposed.get("artist"):
            audio["artist"] = p.proposed["artist"]
        if p.proposed.get("album"):
            audio["album"] = p.proposed["album"]
        audio.save()
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
