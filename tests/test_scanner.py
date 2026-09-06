import shutil
from pathlib import Path

from musictoolkit.db.connection import connect
from musictoolkit.ingest.scanner import scan_library

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _copy_fixture(name: str, dest: Path) -> None:
    shutil.copy(FIXTURES_DIR / name, dest)


def test_scan_adds_new_tracks_and_reads_tags(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    _copy_fixture("complete_tags.mp3", library / "song_a.mp3")
    _copy_fixture("sparse_tags.mp3", library / "song_b.mp3")

    conn = connect(tmp_path / "test.db")
    result = scan_library(conn, library)

    assert result.added == 2
    assert result.updated == 0
    assert result.unchanged == 0

    rows = conn.execute("SELECT * FROM tracks ORDER BY file_path").fetchall()
    assert len(rows) == 2

    tagged = next(r for r in rows if r["file_path"].endswith("song_a.mp3"))
    assert tagged["title"] == "Fixture Title"
    assert tagged["artist"] == "Fixture Artist"
    assert tagged["album"] == "Fixture Album"
    assert tagged["track_number"] == 3
    assert tagged["year"] == 2020

    sparse = next(r for r in rows if r["file_path"].endswith("song_b.mp3"))
    assert sparse["title"] is None
    conn.close()


def test_rescan_unchanged_file_is_a_noop(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    _copy_fixture("complete_tags.mp3", library / "song_a.mp3")

    conn = connect(tmp_path / "test.db")
    scan_library(conn, library)
    result = scan_library(conn, library)

    assert result.added == 0
    assert result.unchanged == 1
    conn.close()


def test_deleted_file_marked_missing_not_removed(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    target = library / "song_a.mp3"
    _copy_fixture("complete_tags.mp3", target)

    conn = connect(tmp_path / "test.db")
    scan_library(conn, library)

    target.unlink()
    result = scan_library(conn, library)

    assert result.missing == 1
    row = conn.execute(
        "SELECT is_missing FROM tracks WHERE file_path = ?", (str(target.resolve()),)
    ).fetchone()
    assert row["is_missing"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM tracks").fetchone()["c"] == 1
    conn.close()


def test_file_reappearing_clears_missing_flag(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    target = library / "song_a.mp3"
    _copy_fixture("complete_tags.mp3", target)

    conn = connect(tmp_path / "test.db")
    scan_library(conn, library)
    target.unlink()
    scan_library(conn, library)

    _copy_fixture("complete_tags.mp3", target)
    scan_library(conn, library)

    row = conn.execute(
        "SELECT is_missing FROM tracks WHERE file_path = ?", (str(target.resolve()),)
    ).fetchone()
    assert row["is_missing"] == 0
    conn.close()
