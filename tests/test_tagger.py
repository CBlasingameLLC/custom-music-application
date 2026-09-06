import shutil
from pathlib import Path

from mutagen.easyid3 import EasyID3

from musictoolkit.db.connection import connect
from musictoolkit.ingest import tagger

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _insert_sparse_track(conn, file_path: str) -> int:
    conn.execute("INSERT INTO tracks (file_path, is_missing) VALUES (?, 0)", (file_path,))
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE file_path = ?", (file_path,)).fetchone()["id"]


def test_propose_tags_uses_best_match(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    track_path = library / "placeholder.mp3"
    track_path.write_bytes(b"not-real-audio")
    _insert_sparse_track(conn, str(track_path.resolve()))

    def fake_best_match(artist: str, title: str):
        return {
            "id": "mb-recording-123",
            "title": "Some Song",
            "ext:score": "95",
            "artist-credit-phrase": "Real Artist",
            "release-list": [{"id": "mb-release-456", "title": "Real Album"}],
        }

    monkeypatch.setattr(tagger.musicbrainz_client, "best_match", fake_best_match)

    result = tagger.propose_tags(conn, library)

    assert len(result.proposals) == 1
    p = result.proposals[0]
    assert p.proposed["title"] == "Some Song"
    assert p.proposed["artist"] == "Real Artist"
    assert p.proposed["album"] == "Real Album"
    assert p.mb_confidence == 0.95
    conn.close()


def test_propose_tags_records_no_match_and_is_resumable(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    track_path = library / "totally obscure track.mp3"
    track_path.write_bytes(b"not-real-audio")
    track_id = _insert_sparse_track(conn, str(track_path.resolve()))

    monkeypatch.setattr(tagger.musicbrainz_client, "best_match", lambda artist, title: None)
    result = tagger.propose_tags(conn, library)

    assert len(result.proposals) == 0
    assert result.skipped_no_match == 1
    row = conn.execute("SELECT tag_source FROM tracks WHERE id = ?", (track_id,)).fetchone()
    assert row["tag_source"] == "musicbrainz_no_match"

    calls = []
    monkeypatch.setattr(
        tagger.musicbrainz_client, "best_match", lambda artist, title: calls.append((artist, title)) or None
    )
    tagger.propose_tags(conn, library)
    assert calls == [], "a track already marked musicbrainz_no_match must not be re-queried"
    conn.close()


def test_apply_tags_writes_file_and_marks_matched(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    track_path = library / "song.mp3"
    shutil.copy(FIXTURES_DIR / "sparse_tags.mp3", track_path)
    track_id = _insert_sparse_track(conn, str(track_path.resolve()))

    def fake_best_match(artist: str, title: str):
        return {"id": "mb-1", "title": "Real Title", "ext:score": "100", "artist-credit-phrase": "Real Artist"}

    monkeypatch.setattr(tagger.musicbrainz_client, "best_match", fake_best_match)
    result = tagger.propose_tags(conn, library)
    tagger.apply_tags(conn, result.proposals)

    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    assert row["title"] == "Real Title"
    assert row["artist"] == "Real Artist"
    assert row["tag_source"] == "musicbrainz"
    assert row["mb_match_confidence"] == 1.0

    on_disk = EasyID3(track_path)
    assert on_disk["title"] == ["Real Title"]
    assert on_disk["artist"] == ["Real Artist"]
    conn.close()
