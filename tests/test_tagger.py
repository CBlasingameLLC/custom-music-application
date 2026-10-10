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


def test_leading_track_numbers_are_not_mistaken_for_an_artist() -> None:
    guess = tagger._guess_from_filename
    assert guess(Path("01 - Glacier.mp3")) == (None, "Glacier")
    assert guess(Path("07. Aurora Vale - Glacier.mp3")) == ("Aurora Vale", "Glacier")
    assert guess(Path("(3) Mara Quinn - Wires.mp3")) == ("Mara Quinn", "Wires")
    assert guess(Path("Disc 2 05 - Drift.mp3")) == (None, "Drift")
    assert guess(Path("Just A Title.mp3")) == (None, "Just A Title")
    assert guess(Path("1999.mp3")) == (None, "1999")  # a bare number is a title, not a prefix


def test_persisted_proposals_wait_for_review_and_are_not_looked_up_twice(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    track_id = _insert_sparse_track(conn, str((library / "a.mp3").resolve()))
    (library / "a.mp3").write_bytes(b"x")

    calls = []

    def fake(artist, title):
        calls.append(title)
        return {"id": "mb-1", "title": "Found It", "ext:score": "88", "artist-credit-phrase": "Someone"}

    monkeypatch.setattr(tagger.musicbrainz_client, "best_match", fake)
    first = tagger.propose_tags(conn, None, persist=True)
    assert len(first.proposals) == 1 and len(calls) == 1
    # waiting for review: the track is untouched and is not offered for a second lookup
    assert conn.execute("SELECT tag_source FROM tracks WHERE id = ?", (track_id,)).fetchone()["tag_source"] is None
    assert tagger.find_sparse_tracks(conn) == []
    tagger.propose_tags(conn, None, persist=True)
    assert len(calls) == 1
    row = conn.execute("SELECT status, confidence FROM tag_proposals WHERE track_id = ?", (track_id,)).fetchone()
    assert (row["status"], row["confidence"]) == ("pending", 0.88)
    conn.close()


def test_progress_and_limit(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    for n in range(5):
        _insert_sparse_track(conn, str((tmp_path / f"{n}.mp3").resolve()))
    monkeypatch.setattr(tagger.musicbrainz_client, "best_match", lambda artist, title: None)
    seen = []
    result = tagger.propose_tags(conn, None, on_progress=lambda d, t: seen.append((d, t)), limit=3)
    assert result.skipped_no_match == 3 and seen[0] == (0, 3) and seen[-1] == (3, 3)
    conn.close()
