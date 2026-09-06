from pathlib import Path

from musictoolkit.db.connection import connect
from musictoolkit.recommend import engine
from musictoolkit.recommend.engine import Candidate


def _insert_track(conn, file_path: str, **overrides) -> int:
    fields = {
        "artist": None, "title": None, "musicbrainz_recording_id": None,
        "musicbrainz_artist_id": None, "is_missing": 0,
    }
    fields.update(overrides)
    columns = ", ".join(["file_path"] + list(fields.keys()))
    placeholders = ", ".join(["?"] * (len(fields) + 1))
    conn.execute(f"INSERT INTO tracks ({columns}) VALUES ({placeholders})", [file_path] + list(fields.values()))
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE file_path = ?", (file_path,)).fetchone()["id"]


def test_filter_owned_drops_exact_mbid_match(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    _insert_track(conn, "/a.mp3", musicbrainz_recording_id="mb-rec-1")
    candidates = [
        Candidate("Artist", "Title", None, "mb-rec-1", "listenbrainz_cf", 1.0, "reason"),
        Candidate("Other Artist", "Other Title", None, "mb-rec-2", "listenbrainz_cf", 1.0, "reason"),
    ]
    result = engine.filter_owned(conn, candidates)
    assert len(result) == 1
    assert result[0].musicbrainz_recording_id == "mb-rec-2"
    conn.close()


def test_filter_owned_drops_normalized_artist_title_match(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    _insert_track(conn, "/a.mp3", artist="The Band", title="A Song!")
    candidates = [
        Candidate("the band", "a song", None, None, "listenbrainz_cf", 1.0, "reason"),
        Candidate("New Artist", "New Song", None, None, "listenbrainz_cf", 1.0, "reason"),
    ]
    result = engine.filter_owned(conn, candidates)
    assert len(result) == 1
    assert result[0].artist_name == "New Artist"
    conn.close()


def test_filter_owned_drops_artist_only_candidate_for_owned_artist(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    _insert_track(conn, "/a.mp3", artist="Radiohead", title="Karma Police")
    candidates = [
        Candidate("radiohead", None, None, None, "lastfm_similar", 0.9, "reason"),
        Candidate("Genuinely New Artist", None, None, None, "lastfm_similar", 0.8, "reason"),
    ]
    result = engine.filter_owned(conn, candidates)
    assert len(result) == 1
    assert result[0].artist_name == "Genuinely New Artist"
    conn.close()


def test_filter_owned_never_drops_a_genuinely_new_candidate(tmp_path: Path) -> None:
    """The single most important failure mode: a real false negative here
    would hide new music from the user, defeating the whole feature."""
    conn = connect(tmp_path / "test.db")
    _insert_track(conn, "/a.mp3", artist="Owned Artist", title="Owned Song", musicbrainz_recording_id="mb-owned")
    candidates = [Candidate("Brand New Artist", "Brand New Song", None, "mb-new", "listenbrainz_cf", 1.0, "reason")]
    result = engine.filter_owned(conn, candidates)
    assert len(result) == 1
    conn.close()


def test_compute_play_history_weights_normalizes_to_max(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    for _ in range(10):
        conn.execute(
            "INSERT INTO play_history (source, played_at_epoch, raw_artist_name) VALUES ('spotify_import', 1, 'Popular Artist')"
        )
    conn.execute(
        "INSERT INTO play_history (source, played_at_epoch, raw_artist_name) VALUES ('spotify_import', 1, 'Rare Artist')"
    )
    conn.commit()

    weights = engine.compute_play_history_weights(conn)
    assert weights["popularartist"] == 1.0
    assert weights["rareartist"] == 0.1
    conn.close()


def test_rank_normalizes_within_source_then_applies_history_bonus() -> None:
    candidates = [
        Candidate("Known Artist", "T1", None, None, "listenbrainz_cf", 50.0, "r"),
        Candidate("Unknown Artist", "T2", None, None, "listenbrainz_cf", 100.0, "r"),
        Candidate("Third Artist", "T3", None, None, "lastfm_similar", 0.5, "r"),
    ]
    weights = {"knownartist": 0.9}

    ranked = engine.rank(candidates, weights)

    # Unknown Artist has the top raw CF score (100 vs 50, normalized 1.0 vs 0.5),
    # but Known Artist's history bonus (0.9) pushes its combined score (0.5+0.9=1.4)
    # above Unknown Artist's (1.0+0=1.0).
    assert ranked[0].artist_name == "Known Artist"
    assert ranked[1].artist_name == "Unknown Artist"
    assert ranked[2].artist_name == "Third Artist"


def test_save_recommendations_dedupes_across_runs(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    candidate = Candidate("Artist", "Song", None, "mb-1", "listenbrainz_cf", 1.0, "reason")

    saved_first = engine.save_recommendations(conn, [candidate])
    saved_second = engine.save_recommendations(conn, [candidate])

    assert saved_first == 1
    assert saved_second == 0
    total = conn.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"]
    assert total == 1
    conn.close()


def test_save_recommendations_dedupes_artist_only_candidates(tmp_path: Path) -> None:
    """Regression guard: track_name is NULL for artist-only candidates, and
    SQL UNIQUE treats every NULL as distinct — without coalescing to '',
    re-running recommend would re-insert the same suggestion forever."""
    conn = connect(tmp_path / "test.db")
    candidate = Candidate("Some Artist", None, None, None, "lastfm_similar", 0.9, "reason")

    saved_first = engine.save_recommendations(conn, [candidate])
    saved_second = engine.save_recommendations(conn, [candidate])

    assert saved_first == 1
    assert saved_second == 0
    conn.close()


def test_top_played_artists_orders_by_play_count(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    for _ in range(5):
        conn.execute(
            "INSERT INTO play_history (source, played_at_epoch, raw_artist_name) VALUES ('spotify_import', 1, 'Most Played')"
        )
    conn.execute(
        "INSERT INTO play_history (source, played_at_epoch, raw_artist_name) VALUES ('spotify_import', 1, 'Least Played')"
    )
    conn.commit()

    top = engine.top_played_artists(conn, limit=1)
    assert top == ["Most Played"]
    conn.close()


def test_fetch_listenbrainz_candidates_resolves_mbids_via_musicbrainz(monkeypatch) -> None:
    monkeypatch.setattr(
        engine.listenbrainz_client,
        "get_cf_recommendations",
        lambda user_name, user_token, count: [{"recording_mbid": "mb-1", "score": 0.8}],
    )
    monkeypatch.setattr(
        engine.musicbrainz_client,
        "get_recording",
        lambda mbid: {
            "title": "Resolved Title",
            "artist-credit-phrase": "Resolved Artist",
            "artist-credit": [{"artist": {"id": "mb-artist-1"}}],
        },
    )

    candidates = engine.fetch_listenbrainz_candidates("some_user", "token", limit=10)

    assert len(candidates) == 1
    assert candidates[0].track_name == "Resolved Title"
    assert candidates[0].artist_name == "Resolved Artist"
    assert candidates[0].musicbrainz_artist_id == "mb-artist-1"
    assert candidates[0].source == "listenbrainz_cf"


def test_fetch_listenbrainz_candidates_skips_unresolvable_mbids(monkeypatch) -> None:
    monkeypatch.setattr(
        engine.listenbrainz_client,
        "get_cf_recommendations",
        lambda user_name, user_token, count: [{"recording_mbid": "mb-gone", "score": 0.5}],
    )
    monkeypatch.setattr(engine.musicbrainz_client, "get_recording", lambda mbid: None)

    candidates = engine.fetch_listenbrainz_candidates("some_user", None, limit=10)
    assert candidates == []


def test_fetch_lastfm_candidates_seeds_from_each_artist(monkeypatch) -> None:
    def fake_get_similar(artist_name, limit):
        return [{"name": f"Similar to {artist_name}", "match": 0.7, "mbid": None}]

    monkeypatch.setattr(engine.lastfm_client, "get_similar_artists", fake_get_similar)

    candidates = engine.fetch_lastfm_candidates(["Seed A", "Seed B"], limit_per_artist=5)

    assert len(candidates) == 2
    assert {c.artist_name for c in candidates} == {"Similar to Seed A", "Similar to Seed B"}
    assert all(c.source == "lastfm_similar" for c in candidates)
