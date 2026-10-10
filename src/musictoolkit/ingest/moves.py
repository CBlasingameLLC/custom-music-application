"""Moving library files safely: companions follow, an undo log is kept, emptied folders are tidied.

Organize and the duplicate finder both move real files, so both go through here:
- a song's lyrics file (.lrc) moves with it, and folder art (cover.jpg and friends) is copied
  to the new folder when it has none, so albums keep their covers;
- every move is written to file_moves with a batch id, which is what Undo replays;
- once a batch is done, folders that held nothing but art and Windows thumbnail files are removed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from musictoolkit.ingest.scanner import AUDIO_EXTENSIONS, QUARANTINE_DIR
from musictoolkit.media.art import FOLDER_ART_EXTS, FOLDER_ART_STEMS

logger = logging.getLogger("musictoolkit")

LYRICS_EXTS = {".lrc"}
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
KEEP_BATCHES = 20  # undoable batches kept per kind

Progress = Callable[[int, int], None]


class MoveError(Exception):
    """A file could not be moved; the message is fit to show to the user."""


def is_folder_art(name: str) -> bool:
    stem, ext = os.path.splitext(name.lower())
    return stem in FOLDER_ART_STEMS and ext in FOLDER_ART_EXTS


def is_junk(name: str) -> bool:
    return name.lower() in JUNK_NAMES


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def on_disk(path: Path) -> Path:
    """The path as the file system spells it. A case-insensitive disk (Windows) keeps the folder names that
    already exist, so a file moved to "Artist/Album" may really sit in "artist/album"; the library has to
    record that spelling, or the next scan would find the same file under a second name."""
    if os.name != "nt":
        return path
    try:
        return Path(os.path.realpath(path))
    except OSError:
        return path


def library_row_at(conn: sqlite3.Connection, path: Path, except_id: int) -> bool:
    """Does the library already list another song at this path? Two rows can never share one (a unique key),
    and a row for a file that went missing still counts: it may carry a rating or play history."""
    collate = " COLLATE NOCASE" if os.name == "nt" else ""
    row = conn.execute(f"SELECT 1 FROM tracks WHERE file_path = ?{collate} AND id != ? LIMIT 1", (str(path), except_id)).fetchone()
    return row is not None


def _rename(old: Path, new: Path) -> None:
    """Move a file. A change of letter case only (a.mp3 -> A.mp3) on a case-insensitive disk is the same
    file by another name, so it goes through a temporary name instead of being refused as "exists"."""
    if new.exists() and same_file(old, new):
        if os.path.normcase(str(old)) != os.path.normcase(str(new)):
            raise MoveError("A file is already there")  # a second name for the same file (a link), not a case change
        parked = old.with_name(old.name + f".{uuid.uuid4().hex[:6]}.moving")
        os.replace(old, parked)
        os.replace(parked, new)
        return
    if new.exists():
        raise MoveError("A file is already there")
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(old), str(new))
    except PermissionError:
        raise MoveError("The file is in use or read-only (is it playing, or open in another program?)") from None
    except OSError as exc:
        raise MoveError(f"{exc.strerror or exc}") from None


def sidecars(old: Path) -> list[Path]:
    """The lyrics file that goes with a song, under its real name (a.lrc, A.LRC, ...), unless another song
    in its folder has the same name apart from the extension (a.mp3 and a.flac share a.lrc): then the lyrics
    belong to that one too and stay put."""
    stem = os.path.normcase(old.stem)
    found: list[Path] = []
    try:
        for entry in old.parent.iterdir():
            if os.path.normcase(entry.stem) != stem or not entry.is_file():
                continue
            suffix = entry.suffix.lower()
            if suffix in AUDIO_EXTENSIONS:
                return []
            if suffix in LYRICS_EXTS:
                found.append(entry)
    except OSError:
        return []
    return found


@dataclass
class BatchResult:
    batch_id: str
    moved: int = 0
    skipped: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    tidied_folders: int = 0

    def absorb(self, other: BatchResult) -> None:
        """Add another result's counts to this one (a batch that spans several library folders)."""
        self.moved += other.moved
        self.skipped += other.skipped
        self.errors += other.errors
        self.tidied_folders += other.tidied_folders

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id, "moved": self.moved, "skipped": self.skipped,
            "errors": self.errors, "tidied_folders": self.tidied_folders,
        }


class Mover:
    """Moves tracks for one batch and logs each move. Call finish() when done to tidy emptied folders."""

    def __init__(
        self, conn: sqlite3.Connection, kind: str, root: Path | None, copy_art: bool = True, hide: bool = False,
        batch_id: str | None = None,
    ) -> None:
        self.conn = conn
        self.kind = kind
        self.root = root
        self.copy_art = copy_art
        self.hide = hide  # quarantine: the library row stays but is hidden (is_missing = 2)
        self.result = BatchResult(batch_id=batch_id or uuid.uuid4().hex[:12])
        self._old_dirs: set[Path] = set()
        self._art_safe: set[Path] = set()  # source folders whose art now also lives at the destination
        self._art_done: set[tuple[Path, Path]] = set()

    def move(self, track_id: int, old: Path, new: Path, extra: Callable[[sqlite3.Connection], dict | None] | None = None) -> bool:
        """Move one track and everything that goes with it. Returns False (and records why) if it was skipped.
        The library row is updated in the same step; if that fails the files are put back. `extra` runs inside
        that same database step and may return one more record for the undo log (the duplicate finder uses it
        to carry plays and playlist entries over to the copy that stays)."""
        if not old.exists():
            self._fail(old, "The file is missing (is its drive connected?)")
            return False
        if library_row_at(self.conn, new, track_id):
            self._fail(old, "The library already lists a different song at that name (a missing file? run a scan first)")
            return False
        try:
            _rename(old, new)
        except MoveError as exc:
            self._fail(old, str(exc))
            return False

        records: list[dict[str, Any]] = []
        for sidecar in sidecars(old):
            target = new.with_suffix(sidecar.suffix)
            try:
                if not target.exists():
                    _rename(sidecar, target)
                    records.append({"kind": "lyrics", "old": str(sidecar), "new": str(target)})
            except MoveError as exc:
                logger.warning("Could not move %s with its song: %s", sidecar, exc)
        if self.copy_art:
            copied = self._copy_art(old.parent, new.parent)
            if copied:
                records.append(copied)

        final = on_disk(new)
        try:
            if extra is not None:
                record = extra(self.conn)
                if record:
                    records.append(record)
            self.conn.execute(
                "UPDATE tracks SET file_path = ?, date_last_scanned = ?, is_missing = ? WHERE id = ?",
                (str(final), _now(), 2 if self.hide else 0, track_id),
            )
            self.conn.execute(
                "INSERT INTO file_moves (batch_id, kind, track_id, old_path, new_path, sidecars_json, moved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.result.batch_id, self.kind, track_id, str(old), str(final), json.dumps(records), _now()),
            )
            self.conn.commit()
        except sqlite3.Error as exc:
            self.conn.rollback()
            logger.error("Could not record the move of %s: %s", old, exc)
            for record in reversed(records):
                _undo_companion(record)
            try:
                _rename(final, old)
            except MoveError:
                logger.error("Could not put %s back at %s", final, old)
            self._fail(old, "The library could not be updated, so the file was left where it was")
            return False
        self._old_dirs.add(old.parent)
        self.result.moved += 1
        return True

    def _fail(self, path: Path, reason: str) -> None:
        self.result.skipped += 1
        self.result.errors.append({"path": str(path), "error": reason})

    def _copy_art(self, old_dir: Path, new_dir: Path) -> dict[str, str] | None:
        if old_dir == new_dir or (old_dir, new_dir) in self._art_done:
            return None
        self._art_done.add((old_dir, new_dir))
        try:
            if any(is_folder_art(e.name) for e in new_dir.iterdir()):
                self._art_safe.add(old_dir)  # the destination already has a cover
                return None
            art = next((e for e in old_dir.iterdir() if e.is_file() and is_folder_art(e.name)), None)
            if art is None:
                self._art_safe.add(old_dir)  # nothing to lose
                return None
            shutil.copy2(art, new_dir / art.name)
        except OSError as exc:
            logger.warning("Could not copy the cover from %s: %s", old_dir, exc)
            return None
        self._art_safe.add(old_dir)
        return {"kind": "art_copy", "old": str(art), "new": str(new_dir / art.name)}

    def finish(self) -> BatchResult:
        """Remove folders this batch emptied, then forget undo batches beyond the newest few."""
        if self.root is not None:
            for folder in sorted(self._old_dirs, key=lambda d: len(d.parts), reverse=True):
                self.result.tidied_folders += prune_empty_folders(folder, self.root, allow_art=folder in self._art_safe)
        _prune_batches(self.conn, self.kind)
        return self.result


def prune_empty_folders(folder: Path, stop_at: Path, allow_art: bool) -> int:
    """Delete `folder`, then its parents, while each holds nothing but thumbnail files. Cover art counts as
    removable only in `folder` itself and only if allowed (it was copied or is not needed); a parent's
    folder.jpg may be an artist picture, so a parent holding one stays. `stop_at` (the library folder) is
    never removed, and nothing above it is touched."""
    removed = 0
    stop = os.path.normcase(os.path.normpath(str(stop_at)))
    inside = os.path.normcase(os.path.normpath(str(folder))).startswith(stop + os.sep)
    while inside and folder.is_dir():
        try:
            entries = list(folder.iterdir())
        except OSError:
            break
        if any(e.is_dir() or not (is_junk(e.name) or (allow_art and is_folder_art(e.name))) for e in entries):
            break
        try:
            for entry in entries:
                entry.unlink()
            folder.rmdir()
        except OSError:
            break
        removed += 1
        folder = folder.parent
        allow_art = False
        inside = os.path.normcase(os.path.normpath(str(folder))).startswith(stop + os.sep)
    return removed


def has_audio(folder: Path) -> bool:
    try:
        return any(e.is_file() and e.suffix.lower() in AUDIO_EXTENSIONS for e in folder.iterdir())
    except OSError:
        return False


# ---------------------------------------------------------------------------------------------- undo


def undo_batch(
    conn: sqlite3.Connection, batch_id: str, kind: str, roots: list[Path], on_progress: Progress | None = None
) -> BatchResult:
    """Put the files of a batch back where they were. A file that is gone, or whose old spot is taken, is
    left alone and reported; the rest are restored. Emptied folders are tidied only inside `roots`."""
    rows = conn.execute(
        "SELECT * FROM file_moves WHERE batch_id = ? AND kind = ? AND undone_at IS NULL ORDER BY id DESC", (batch_id, kind)
    ).fetchall()
    return _undo_rows(conn, rows, BatchResult(batch_id=batch_id), roots, on_progress)


def undo_tracks(
    conn: sqlite3.Connection, track_ids: list[int], kind: str, roots: list[Path], on_progress: Progress | None = None
) -> BatchResult:
    """Undo just these songs' most recent move, whichever batch it was in. A quarantined song whose log entry
    has been forgotten still goes home: the review folder mirrors the library, so its old place can be read
    off its path."""
    ids = list(dict.fromkeys(track_ids))
    found: list[sqlite3.Row] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        found += conn.execute(
            f"SELECT * FROM file_moves WHERE kind = ? AND undone_at IS NULL AND track_id IN ({','.join('?' * len(chunk))})",
            [kind, *chunk],
        ).fetchall()
    found.sort(key=lambda r: r["id"], reverse=True)
    latest: dict[int, Any] = {}
    for row in found:
        latest.setdefault(row["track_id"], row)
    if kind == "quarantine":
        for track_id in ids:
            if track_id in latest:
                continue
            row = conn.execute("SELECT id, file_path FROM tracks WHERE id = ? AND is_missing = 2", (track_id,)).fetchone()
            home = home_of_quarantined(Path(row["file_path"])) if row else None
            if home is not None:
                latest[track_id] = {"id": None, "track_id": track_id, "old_path": str(home), "new_path": row["file_path"], "sidecars_json": "[]"}
    return _undo_rows(conn, list(latest.values()), BatchResult(batch_id="restore"), roots, on_progress)


def home_of_quarantined(path: Path) -> Path | None:
    """`<library>/_duplicates_review/Artist/Album/x.mp3` -> `<library>/Artist/Album/x.mp3`."""
    parts = path.parts
    for index, part in enumerate(parts[:-1]):
        if part.lower() == QUARANTINE_DIR and index + 1 < len(parts):
            return Path(*parts[:index], *parts[index + 1:])
    return None


def _undo_rows(
    conn: sqlite3.Connection, rows: list[Any], result: BatchResult, roots: list[Path], on_progress: Progress | None
) -> BatchResult:
    new_dirs: set[Path] = set()
    try:
        for done, row in enumerate(rows):
            if on_progress:
                on_progress(done, len(rows))
            old, new = Path(row["old_path"]), Path(row["new_path"])
            if not new.exists():
                result.skipped += 1
                result.errors.append({"path": str(new), "error": "The file is no longer where it was moved to"})
                continue
            if old.exists() and not same_file(old, new):
                result.skipped += 1
                result.errors.append({"path": str(old), "error": "Something else is already at the original location"})
                continue
            if library_row_at(conn, old, row["track_id"]):
                result.skipped += 1
                result.errors.append({"path": str(old), "error": "The library lists a different song at the original location"})
                continue
            try:
                _rename(new, old)
            except MoveError as exc:
                result.skipped += 1
                result.errors.append({"path": str(new), "error": str(exc)})
                continue
            for record in json.loads(row["sidecars_json"] or "[]"):
                if record["kind"] == "merge":
                    unmerge(conn, row["track_id"], record)
                else:
                    _undo_companion(record)
            new_dirs.add(new.parent)
            conn.execute("UPDATE tracks SET file_path = ?, is_missing = 0 WHERE id = ?", (str(old), row["track_id"]))
            if row["id"] is not None:
                conn.execute("UPDATE file_moves SET undone_at = ? WHERE id = ?", (_now(), row["id"]))
            conn.commit()
            result.moved += 1
        if on_progress:
            on_progress(len(rows), len(rows))
    finally:  # a cancelled undo still tidies the folders it emptied
        for folder in sorted(new_dirs, key=lambda d: len(d.parts), reverse=True):
            root = root_of(folder, roots)
            if root is not None and not has_audio(folder):
                result.tidied_folders += prune_empty_folders(folder, root, allow_art=True)
    return result


def root_of(folder: Path, roots: list[Path]) -> Path | None:
    """The deepest library folder that contains `folder`."""
    inside = [r for r in roots if os.path.normcase(os.path.normpath(str(folder))).startswith(os.path.normcase(os.path.normpath(str(r))) + os.sep)]
    return max(inside, key=lambda r: len(r.parts), default=None)


def _undo_companion(record: dict[str, Any]) -> None:
    if record.get("kind") not in ("lyrics", "art_copy"):
        return
    old, new = Path(record["old"]), Path(record["new"])
    try:
        if record["kind"] == "lyrics":
            if new.exists() and not old.exists():
                _rename(new, old)
        elif record["kind"] == "art_copy":
            if not old.exists():
                old.parent.mkdir(parents=True, exist_ok=True)
                if new.exists():
                    shutil.copy2(new, old)  # the original folder was tidied away; give its cover back
    except (MoveError, OSError) as exc:
        logger.warning("Could not restore %s: %s", old, exc)


def unmerge(conn: sqlite3.Connection, dup_id: int, record: dict[str, Any]) -> None:
    """Give a restored copy back the plays, playlist entries, rating and favorite that moved to the copy that
    stayed. Only what is still where the merge left it is moved back; the caller commits."""
    keeper = record["keeper"]
    for start in range(0, len(record.get("plays", [])), 500):
        chunk = record["plays"][start:start + 500]
        conn.execute(
            f"UPDATE play_history SET track_id = ? WHERE track_id = ? AND id IN ({','.join('?' * len(chunk))})", [dup_id, keeper, *chunk]
        )
    for rowid in record.get("playlist_moved", []):
        conn.execute("UPDATE playlist_tracks SET track_id = ? WHERE rowid = ? AND track_id = ?", (dup_id, rowid, keeper))
    for entry in record.get("playlist_dropped", []):
        exists = conn.execute(
            "SELECT 1 FROM playlists WHERE id = ?", (entry["playlist_id"],)
        ).fetchone() and not conn.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (entry["playlist_id"], dup_id)
        ).fetchone()
        if exists:
            conn.execute(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                (entry["playlist_id"], dup_id, entry["position"]),
            )
    for column in ("rating", "favorite"):
        change = record.get(column)
        if change:
            conn.execute(f"UPDATE tracks SET {column} = ? WHERE id = ? AND {column} IS ?", (change["before"], keeper, change["after"]))


def list_batches(conn: sqlite3.Connection, kind: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT batch_id, MIN(moved_at) AS moved_at, COUNT(*) AS files, SUM(undone_at IS NULL) AS undoable "
        "FROM file_moves WHERE kind = ? GROUP BY batch_id ORDER BY MAX(id) DESC LIMIT ?",
        (kind, limit),
    ).fetchall()
    return [
        {"batch_id": r["batch_id"], "moved_at": r["moved_at"], "files": r["files"], "undoable": r["undoable"] or 0}
        for r in rows
    ]


def _prune_batches(conn: sqlite3.Connection, kind: str) -> None:
    old = conn.execute(
        "SELECT batch_id FROM file_moves WHERE kind = ? GROUP BY batch_id ORDER BY MAX(id) DESC LIMIT -1 OFFSET ?",
        (kind, KEEP_BATCHES),
    ).fetchall()
    if old:
        conn.executemany("DELETE FROM file_moves WHERE batch_id = ?", [(r["batch_id"],) for r in old])
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
