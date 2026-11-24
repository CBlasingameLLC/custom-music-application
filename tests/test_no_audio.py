"""Files that are only tags: the scan finds them quietly, the library marks them, and nothing tries to play them."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from musictoolkit.db import connection
from musictoolkit.db.connection import connect
from musictoolkit.ingest import scanner, tagedit
from musictoolkit.ingest.scanner import NO_AUDIO_BELOW, add_files, scan_library
from musictoolkit.sync.selector import resolve_selection
from musictoolkit.web.routers.playback import NO_AUDIO_MESSAGE
from tests.conftest import FIXTURES_DIR, build_web, make_stub, make_track, scan

PADDING = bytes([1, 2, 3, 4]) * 4096  # (no 0xFF in it, so nothing in it can pass for the start of an MPEG frame)


def syncsafe(number: int) -> bytes:
    return bytes([(number >> 21) & 0x7F, (number >> 14) & 0x7F, (number >> 7) & 0x7F, number & 0x7F])


def rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {Path(r["file_path"]).name: r for r in conn.execute("SELECT * FROM tracks")}


def messages_of(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "musictoolkit"]


# ---------------------------------------------------------------------------------------------------- the scan


def test_a_file_that_is_only_a_tag_is_added_with_its_tags_marked_and_counted_apart_from_the_errors(tmp_path: Path) -> None:
    library = tmp_path / "library"
    make_stub(library)
    shutil.copy(FIXTURES_DIR / "complete_tags.mp3", library / "real.mp3")
    conn = connect(tmp_path / "test.db")
    result = scan_library(conn, library)
    assert (result.added, result.no_audio, result.errors, result.unchanged) == (2, 1, 0, 0)
    stub, real = rows(conn)["stub.mp3"], rows(conn)["real.mp3"]
    assert (stub["no_audio"], real["no_audio"]) == (1, 0)
    assert (stub["title"], stub["artist"], stub["album"], stub["track_number"]) == ("Hidden Song", "Some Band", "Some Album", 4)  # (what the tag says is kept)
    assert stub["duration_seconds"] is None and stub["bitrate"] is None and stub["format"] == "mp3" and stub["is_missing"] == 0
    assert real["duration_seconds"] and real["title"] == "Fixture Title"
    conn.close()


def test_a_scan_that_finds_such_files_writes_nothing_above_debug_and_never_a_traceback(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    library = tmp_path / "library"
    for number in range(5):
        make_stub(library, f"stub{number}.mp3", title=f"Stub {number}")
    conn = connect(tmp_path / "test.db")
    with caplog.at_level(logging.INFO, logger="musictoolkit"):
        scan_library(conn, library)
    assert messages_of(caplog) == []  # (a log of these would hide real trouble)
    caplog.clear()
    conn.execute("UPDATE tracks SET file_mtime = NULL")
    conn.commit()
    with caplog.at_level(logging.DEBUG, logger="musictoolkit"):
        result = scan_library(conn, library)
    assert result.no_audio == 5 and result.updated == 5
    records = messages_of(caplog)
    assert len(records) == 5 and all(r.exc_info is None and r.levelno == logging.DEBUG and "has tags but no audio in it" in r.getMessage() for r in records)
    conn.close()


def _only_tags_plus(directory: Path, name: str, extra: bytes = b"", *, twice: bool = False, v1: bool = False) -> Path:
    path = make_stub(directory, name)
    raw = path.read_bytes()
    path.write_bytes((raw * 2 if twice else raw) + extra + (b"TAG" + bytes(125) if v1 else b""))
    return path


@pytest.mark.parametrize("build", [
    pytest.param(lambda d: _only_tags_plus(d, "a.mp3"), id="a tag"),
    pytest.param(lambda d: _only_tags_plus(d, "a.mp3", v1=True), id="a tag at each end"),
    pytest.param(lambda d: _only_tags_plus(d, "a.mp3", twice=True), id="two tags"),
    pytest.param(lambda d: _only_tags_plus(d, "a.mp3", twice=True, v1=True), id="two tags and one at the end"),
    pytest.param(lambda d: _only_tags_plus(d, "a.mp3", PADDING[:4095]), id="a tag and almost nothing"),
    pytest.param(lambda d: (d.mkdir(parents=True, exist_ok=True), (d / "a.mp3").write_bytes(b""), d / "a.mp3")[2], id="an empty file"),
    pytest.param(lambda d: (d.mkdir(parents=True, exist_ok=True), (d / "a.mp3").write_bytes(PADDING[:4095]), d / "a.mp3")[2], id="almost nothing"),
    pytest.param(lambda d: _only_tags_plus(d, "A.MP3", PADDING[:300]), id="capitals in the extension"),
    pytest.param(lambda d: (d.mkdir(parents=True, exist_ok=True), (d / "a.flac").write_bytes(b""), d / "a.flac")[2], id="an empty flac"),
])
def test_what_has_no_audio_in_it(tmp_path: Path, build) -> None:
    library = tmp_path / "library"
    build(library)
    conn = connect(tmp_path / "test.db")
    result = scan_library(conn, library)
    assert (result.added, result.no_audio, result.errors) == (1, 1, 0)
    assert conn.execute("SELECT no_audio FROM tracks").fetchone()[0] == 1
    conn.close()


def test_a_file_with_more_than_a_tag_that_cannot_be_read_is_an_error_but_not_a_file_without_audio(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    library = tmp_path / "library"
    _only_tags_plus(library, "a.mp3", PADDING[:4096])  # (exactly the least that is not "nothing": it might be damaged audio, so it is not called empty)
    conn = connect(tmp_path / "test.db")
    with caplog.at_level(logging.INFO, logger="musictoolkit"):
        result = scan_library(conn, library)
    assert (result.added, result.no_audio, result.errors) == (1, 0, 1)
    row = rows(conn)["a.mp3"]
    assert row["no_audio"] == 0 and row["title"] == "Hidden Song" and row["duration_seconds"] is None  # (the file stays in the library, and its tag says what the song is)
    [record] = messages_of(caplog)
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert "Could not read the audio in" in record.getMessage() and "a.mp3" in record.getMessage() and "sync to MPEG frame" in record.getMessage()
    conn.close()


def test_a_file_of_a_kind_nothing_knows_is_an_error_with_one_line_of_log_and_a_file_that_cannot_be_opened_too(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "odd.m4a").write_bytes(PADDING[:6000])
    conn = connect(tmp_path / "test.db")
    with caplog.at_level(logging.INFO, logger="musictoolkit"):
        result = scan_library(conn, library)
    assert (result.added, result.no_audio, result.errors) == (1, 0, 1) and rows(conn)["odd.m4a"]["no_audio"] == 0
    [record] = messages_of(caplog)
    assert record.levelno == logging.WARNING and record.exc_info is None and "odd.m4a: it is not a kind of audio file this app knows" in record.getMessage()

    def refuses(path, easy=False):
        raise PermissionError(13, "Permission denied")

    caplog.clear()
    (library / "locked.mp3").write_bytes(PADDING[:6000])
    monkeypatch.setattr(scanner.mutagen, "File", refuses)
    with caplog.at_level(logging.INFO, logger="musictoolkit"):
        result = scan_library(conn, library)
    assert (result.added, result.unchanged, result.errors, result.no_audio) == (1, 1, 1, 0)  # (the new one cannot be opened; the other is not read again, it has not changed)
    assert {r.levelno for r in messages_of(caplog)} == {logging.WARNING} and all(r.exc_info is None for r in messages_of(caplog))
    assert any("Permission denied" in r.getMessage() for r in messages_of(caplog))
    conn.close()


def test_the_least_that_is_a_tag_and_nothing_else_is_counted_by_the_bytes_of_each_part(tmp_path: Path) -> None:
    assert NO_AUDIO_BELOW == 4096  # (a second of MP3 is 4000 bytes at the lowest bitrate, 16000 at the usual one)
    audio_bytes = scanner._audio_bytes
    capitals = make_stub(tmp_path / "caps", "A.MP3")
    capitals.write_bytes(capitals.read_bytes() + PADDING[:300])
    assert audio_bytes(capitals, capitals.stat().st_size) == 300  # (an extension in capitals is an MP3's all the same)
    stub = make_stub(tmp_path)
    size = stub.stat().st_size
    assert audio_bytes(stub, size) == 0
    stub.write_bytes(stub.read_bytes() + PADDING[:300])
    assert audio_bytes(stub, size + 300) == 300
    stub.write_bytes(stub.read_bytes() + b"TAG" + bytes(125))
    assert audio_bytes(stub, size + 300 + 128) == 300  # (an ID3v1 tag is not audio)
    other = tmp_path / "other.mp3"
    other.write_bytes(b"TAG" + bytes(125))
    assert audio_bytes(other, 128) == 0
    shorter = tmp_path / "shorter.mp3"
    shorter.write_bytes(b"TAG" + bytes(10))
    assert audio_bytes(shorter, 13) == 13  # (too short to hold one at its end)
    near = tmp_path / "near.mp3"
    near.write_bytes(PADDING[:100] + b"TAG" + bytes(125))
    assert audio_bytes(near, 228) == 100
    # an ID3v2 tag that says it has a footer: header 10, body 100, footer 10, then the rest
    footer = tmp_path / "footer.mp3"
    footer.write_bytes(b"ID3\x04\x00\x10" + syncsafe(100) + bytes(100) + b"3DI\x04\x00\x10" + syncsafe(100) + PADDING[:50])
    assert audio_bytes(footer, 170) == 50
    plain = tmp_path / "plain.mp3"
    plain.write_bytes(b"ID3\x04\x00\x00" + syncsafe(100) + bytes(100) + PADDING[:50])
    assert audio_bytes(plain, 160) == 50
    # the length is four bytes of seven bits each
    big = tmp_path / "big.mp3"
    body = 0b1010101_0110011_1100110_0001111
    assert body == 22_000_000 or body > 4_000_000  # (a number that needs all four bytes)
    big.write_bytes(b"ID3\x04\x00\x00" + syncsafe(body) + PADDING[:10])
    assert audio_bytes(big, 10 + 10) == 0 and audio_bytes(big, 10 + body + 77) == 77
    # only an MP3 has tags that can be all there is
    flac = tmp_path / "x.flac"
    flac.write_bytes(b"ID3\x04\x00\x00" + syncsafe(5) + bytes(5))
    assert audio_bytes(flac, 20) == 20
    assert audio_bytes(tmp_path / "gone.mp3", 123) == 123  # (a file that cannot be looked into is not called empty)
    two = tmp_path / "two.mp3"
    one_tag = make_stub(tmp_path / "one").read_bytes()
    two.write_bytes(one_tag + one_tag + PADDING[:300])
    assert audio_bytes(two, 2 * len(one_tag) + 300) == 300  # (tags one after another are all tags)
    overlap = tmp_path / "overlap.mp3"  # an ID3v2 tag with "TAG" inside it where an ID3v1 tag would begin: the tag is not at the end of the file, the audio is
    overlap.write_bytes(b"ID3\x04\x00\x00" + syncsafe(200) + bytes(122) + b"TAG" + bytes(75) + PADDING[:50])
    assert overlap.stat().st_size == 260 and audio_bytes(overlap, 260) == 50
    cut = tmp_path / "cut.mp3"  # a tag that says it is longer than the file
    cut.write_bytes(b"ID3\x04\x00\x00" + syncsafe(1000) + bytes(40))
    assert audio_bytes(cut, 50) == 0
    assert scanner._id3v2_length(b"ID3\x04\x00\x00" + syncsafe(7)) == 17 and scanner._id3v2_length(b"ID3") == 0 and scanner._id3v2_length(b"XYZ" + bytes(7)) == 0
    assert scanner._id3v2_length((b"ID3\x04\x00\x00" + syncsafe(7))[:9]) == 0  # (a header is ten bytes: nine are not one)
    assert scanner._id3v2_length(b"") == 0


def test_a_file_that_is_only_tags_is_read_again_when_it_changes_and_not_otherwise(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    library = tmp_path / "library"
    stub = make_stub(library, "song.mp3")
    conn = connect(tmp_path / "test.db")
    scan_library(conn, library)
    again = scan_library(conn, library)
    assert (again.unchanged, again.no_audio, again.updated) == (1, 0, 0)  # (not read again: nothing new to say about it)
    shutil.copy(FIXTURES_DIR / "complete_tags.mp3", stub)  # the song arrives in the file that held its tags
    grown = scan_library(conn, library)
    assert (grown.updated, grown.no_audio) == (1, 0)
    row = rows(conn)["song.mp3"]
    assert row["no_audio"] == 0 and row["duration_seconds"] and row["title"] == "Fixture Title"
    make_stub(library, "song.mp3", title="Back To Tags")  # and it is only tags again
    shrunk = scan_library(conn, library)
    assert (shrunk.updated, shrunk.no_audio) == (1, 1)
    assert rows(conn)["song.mp3"]["no_audio"] == 1 and rows(conn)["song.mp3"]["title"] == "Back To Tags"
    conn.close()


def test_a_song_opened_from_outside_the_library_is_marked_the_same_way_and_the_tag_editor_keeps_the_mark_true(tmp_path: Path) -> None:
    stub = make_stub(tmp_path / "elsewhere")
    conn = connect(tmp_path / "test.db")
    [(_, track_id, outcome)] = add_files(conn, [str(stub)])
    assert outcome == "added" and conn.execute("SELECT no_audio FROM tracks WHERE id = ?", (track_id,)).fetchone()[0] == 1
    shutil.copy(FIXTURES_DIR / "complete_tags.mp3", stub)
    tagedit.refresh_from_file(conn, track_id, stub)
    assert conn.execute("SELECT no_audio, duration_seconds FROM tracks WHERE id = ?", (track_id,)).fetchone()[0] == 0
    make_stub(tmp_path / "elsewhere")
    tagedit.refresh_from_file(conn, track_id, stub)
    assert conn.execute("SELECT no_audio FROM tracks WHERE id = ?", (track_id,)).fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------------------------------- the database


def test_a_database_from_before_has_its_songs_without_a_length_read_again_once_and_the_others_left_as_they_are(tmp_path: Path) -> None:
    library = tmp_path / "library"
    make_stub(library, "stub.mp3")
    shutil.copy(FIXTURES_DIR / "complete_tags.mp3", library / "real.mp3")
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    for version, _name, sql in connection._migration_files():
        if version <= 15:
            old.executescript(sql)
    old.execute("PRAGMA user_version = 15")
    stub_stat, real_stat = (library / "stub.mp3").stat(), (library / "real.mp3").stat()
    for path, stat, duration, missing in (
        (library / "stub.mp3", stub_stat, None, 0), (library / "real.mp3", real_stat, 12.0, 0), (library / "gone.mp3", real_stat, None, 1),
    ):
        old.execute(
            "INSERT INTO tracks (file_path, file_size, file_mtime, duration_seconds, format, is_missing) VALUES (?, ?, ?, ?, 'mp3', ?)",
            (str(path), stat.st_size, stat.st_mtime, duration, missing),
        )
    old.commit()
    old.close()

    conn = connect(db)  # (the app starts: migration 16)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 16
    by_name = rows(conn)
    assert {name: row["no_audio"] for name, row in by_name.items()} == {"stub.mp3": 0, "real.mp3": 0, "gone.mp3": 0}
    assert by_name["stub.mp3"]["file_mtime"] is None  # (read again at the next scan)
    assert by_name["real.mp3"]["file_mtime"] == real_stat.st_mtime  # (a song with a length is not)
    assert by_name["gone.mp3"]["file_mtime"] == real_stat.st_mtime  # (nor one whose file is not there)
    result = scan_library(conn, library, force=True)
    assert (result.updated, result.unchanged, result.no_audio) == (1, 1, 1)
    assert rows(conn)["stub.mp3"]["no_audio"] == 1 and rows(conn)["stub.mp3"]["title"] == "Hidden Song"
    conn.close()


# ---------------------------------------------------------------------------------------------------- the app


@pytest.fixture
def stubbed(tmp_path: Path, music_dir: Path):
    make_stub(music_dir / "Stubs" / "Empty Album", "01 - Hidden Song.mp3")
    web = build_web(tmp_path, music_dir)
    result = scan(web)
    assert result["no_audio"] == 1 and result["errors"] == 0 and result["added"] == 10
    web.stub_id = web.client.get("/api/tracks", params={"rules": json.dumps({"match": "all", "rules": [{"field": "title", "op": "is", "value": "Hidden Song"}]})}).json()["items"][0]["id"]
    return web


def test_the_library_lists_a_file_that_is_only_tags_with_the_mark_and_a_filter_finds_exactly_those(stubbed) -> None:
    everything = stubbed.client.get("/api/tracks", params={"limit": 100}).json()
    assert everything["total"] == 10
    assert {t["title"] for t in everything["items"] if t["no_audio"]} == {"Hidden Song"}
    assert sum(1 for t in everything["items"] if not t["no_audio"]) == 9
    rule = {"match": "all", "rules": [{"field": "no_audio", "op": "is", "value": True}]}
    found = stubbed.client.get("/api/tracks", params={"rules": json.dumps(rule)}).json()
    assert [t["id"] for t in found["items"]] == [stubbed.stub_id] and found["total"] == 1
    rule["rules"][0]["value"] = False  # (the filter's other side: "not: has tags but no audio")
    others = stubbed.client.get("/api/tracks", params={"rules": json.dumps(rule), "limit": 100}).json()
    assert others["total"] == 9 and stubbed.stub_id not in {t["id"] for t in others["items"]}


def test_playing_a_file_that_is_only_tags_is_refused_in_words_and_nothing_else_is_(stubbed) -> None:
    refused = stubbed.client.get(f"/api/tracks/{stubbed.stub_id}/stream")
    assert refused.status_code == 422 and refused.json() == {"detail": NO_AUDIO_MESSAGE} == {"detail": "This file has tags but no audio in it, so there is nothing to play."}
    assert stubbed.client.head(f"/api/tracks/{stubbed.stub_id}/stream").status_code == 422
    details = stubbed.client.get(f"/api/tracks/{stubbed.stub_id}/info").json()
    assert (details["playable"], details["no_audio"]) == (False, True)
    other = next(t["id"] for t in stubbed.client.get("/api/tracks", params={"limit": 100}).json()["items"] if not t["no_audio"])
    assert stubbed.client.get(f"/api/tracks/{other}/stream").status_code == 200
    info = stubbed.client.get(f"/api/tracks/{other}/info").json()
    assert (info["playable"], info["no_audio"]) == (True, False)
    assert stubbed.client.get(f"/api/tracks/{stubbed.stub_id}/lyrics").status_code == 200  # (its tags are still its own: only playing it is refused)
    assert stubbed.client.get("/api/tracks/999999/stream").status_code == 404


def test_a_file_without_audio_whose_drive_is_gone_is_still_the_drive_that_is_reported(stubbed, music_dir: Path) -> None:
    for path in music_dir.rglob("*.mp3"):
        path.unlink()
    for folder in sorted(music_dir.rglob("*"), reverse=True):
        folder.rmdir()
    assert stubbed.client.get(f"/api/tracks/{stubbed.stub_id}/stream").status_code == 503


def test_mixes_and_the_songs_chosen_for_a_device_leave_a_file_without_audio_out(stubbed) -> None:
    mix = stubbed.client.get("/api/mixes/unheard", params={"size": 50}).json()
    assert len(mix["tracks"]) == 9 and stubbed.stub_id not in {t["id"] for t in mix["tracks"]}
    assert all(not t["no_audio"] for t in mix["tracks"])
    conn = connect(stubbed.config.database.path)
    chosen = resolve_selection(conn, select_all=True)
    assert len(chosen) == 9 and "Hidden Song" not in {r["title"] for r in chosen}
    conn.execute("INSERT INTO playlists (name) VALUES ('P')")
    pid = conn.execute("SELECT id FROM playlists").fetchone()[0]
    for position, row in enumerate(conn.execute("SELECT id FROM tracks ORDER BY id").fetchall()):
        conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)", (pid, row[0], position))
    conn.execute("UPDATE tracks SET genre = 'Mixed', rating = 5")
    conn.commit()
    for kwargs in ({"playlist_name": "P"}, {"tag_filter": "Mixed"}, {"min_rating": 5}):
        got = resolve_selection(conn, **kwargs)
        assert len(got) == 9 and stubbed.stub_id not in {r["id"] for r in got}, kwargs
    conn.close()


def test_diagnostics_count_them(stubbed) -> None:
    assert stubbed.client.get("/api/system/diagnostics").json()["database"]["no_audio"] == 1
    assert stubbed.client.get("/api/about").json()["tracks"] == 10  # (they are in the library: only the playing is left out)


def test_a_tag_only_file_gives_no_trouble_to_the_real_server_log_either(tmp_path: Path, music_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    make_stub(music_dir / "Stubs" / "A", "x.mp3")
    web = build_web(tmp_path, music_dir)
    with caplog.at_level(logging.INFO, logger="musictoolkit"):
        scan(web)
    assert [r.getMessage() for r in messages_of(caplog)] == []
    assert isinstance(web.client, TestClient)


def test_a_file_that_goes_away_while_it_is_being_read_is_one_that_could_not_be_read_and_not_one_without_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuses(path, easy=False):
        raise scanner.mutagen.MutagenError("cannot read")

    monkeypatch.setattr(scanner.mutagen, "File", refuses)
    fields = scanner._read_basic_tags(tmp_path / "gone.mp3")  # (not there at all: nothing is known of its size)
    assert (fields["no_audio"], fields["unreadable"], fields["title"]) == (False, True, None)


def test_a_file_without_audio_does_not_make_a_mix_worth_a_card_and_is_not_in_a_song_radio(tmp_path: Path, music_dir: Path) -> None:
    from musictoolkit.web import mixes

    folder = music_dir / "Seven"
    for number in range(7):  # (eight songs of a genre or a decade make a mix; seven do not)
        make_track(folder, f"{number}.mp3", title=f"Seven {number}", artist="Seven Artist", album="Sevens", genre="Zydeco", date="1987", tracknumber=number + 1)
    make_stub(folder, "stub.mp3", title="Eighth", artist="Seven Artist", album="Sevens", genre="Zydeco", year="1987")
    web = build_web(tmp_path, music_dir)
    scan(web)
    conn = connect(web.config.database.path)
    offered = lambda: {mix["id"] for mix in mixes.available(conn)}  # noqa: E731
    assert "decade-1980" not in offered() and mixes.genre_id("Zydeco") not in offered()
    conn.execute("UPDATE tracks SET no_audio = 0")  # (the same eight with sound in all of them are a card each)
    conn.commit()
    assert {"decade-1980", mixes.genre_id("Zydeco")} <= offered()
    conn.execute("UPDATE tracks SET no_audio = 1 WHERE title = 'Eighth'")
    conn.commit()
    seed = conn.execute("SELECT id FROM tracks WHERE title = 'Seven 0'").fetchone()[0]
    stub = conn.execute("SELECT id FROM tracks WHERE title = 'Eighth'").fetchone()[0]
    others = [r[0] for r in conn.execute("SELECT id FROM tracks WHERE title LIKE 'Seven %' AND id <> ?", (seed,))]
    now = 1_700_000_000  # (the stub and five others were "played together" with the seed: a sitting)
    for offset, track in enumerate([seed, stub, *others[:5]]):
        conn.execute("INSERT INTO play_history (track_id, source, played_at_epoch) VALUES (?, 'future_scrobble', ?)", (track, now + offset * 60))
    conn.commit()
    radio = web.client.get(f"/api/radio/track/{seed}", params={"size": 40}).json()["tracks"]
    assert stub not in {t["id"] for t in radio} and seed in {t["id"] for t in radio} and len(radio) >= 6  # (by the same genre and artist, and by the sitting: it would be first)
    conn.execute("UPDATE tracks SET no_audio = 0 WHERE id = ?", (stub,))
    conn.commit()
    assert stub in {t["id"] for t in web.client.get(f"/api/radio/track/{seed}", params={"size": 40}, headers={}).json()["tracks"]}  # (and with sound it is)
    conn.close()


def test_a_file_of_another_kind_that_is_only_an_id3_tag_is_a_file_without_audio_with_its_tags(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    stub = make_stub(library, "song.flac", title="In A Flac", artist="Someone")  # (an ID3 tag in a file that says it is a FLAC)
    conn = connect(tmp_path / "test.db")
    result = scan_library(conn, library)
    assert (result.added, result.no_audio, result.errors) == (1, 1, 0)
    row = rows(conn)[stub.name]
    assert (row["no_audio"], row["title"], row["artist"], row["format"]) == (1, "In A Flac", "Someone", "flac")
    conn.close()


def test_a_song_that_is_gone_is_no_longer_counted_among_the_files_without_audio(stubbed, music_dir: Path) -> None:
    assert stubbed.client.get("/api/system/diagnostics").json()["database"]["no_audio"] == 1
    next(music_dir.rglob("01 - Hidden Song.mp3")).unlink()
    scan(stubbed)
    database = stubbed.client.get("/api/system/diagnostics").json()["database"]
    assert (database["no_audio"], database["missing"]) == (0, 1)


@pytest.mark.parametrize("name", ["song.wma", "song.ape"])
def test_a_kind_of_file_a_browser_cannot_play_is_not_playable_and_is_not_said_to_have_no_audio(stubbed, music_dir: Path, name: str) -> None:
    (music_dir / name).write_bytes(PADDING[:6000])
    scan(stubbed)
    found = stubbed.client.get("/api/tracks", params={"limit": 100}).json()["items"]
    track = next(t for t in found if t["format"] == name.split(".")[1])
    info = stubbed.client.get(f"/api/tracks/{track['id']}/info").json()
    assert (info["playable"], info["no_audio"]) == (False, False)


def test_a_genre_or_a_decade_of_files_without_audio_does_not_take_the_place_of_one_with_songs_in_it(tmp_path: Path, music_dir: Path) -> None:
    from musictoolkit.web import mixes

    folder = music_dir / "Many"
    for genre in range(6):  # (six genres of nine songs, over four decades: each a card)
        for number in range(9):
            make_track(folder / f"g{genre}", f"{number}.mp3", title=f"G{genre} {number}", artist=f"Artist {genre}", album=f"Album {genre}", genre=f"Genre{genre}", date=str(1963 + 10 * (number % 4)), tracknumber=number + 1)
    for number in range(7):  # (a seventh genre and a fifth decade: seven songs, too few for a card, and seven files without audio, which would make it the biggest)
        make_track(folder / "seven", f"{number}.mp3", title=f"S {number}", artist="Artist 7", album="Album 7", genre="Genre6", date="2005", tracknumber=number + 1)
        make_stub(folder / "seven", f"stub{number}.mp3", title=f"Stub {number}", artist="Artist 7", album="Album 7", genre="Genre6", year="2005")
    web = build_web(tmp_path, music_dir)
    scan(web)
    conn = connect(web.config.database.path)
    offered = {mix["id"] for mix in mixes.available(conn, size=50)}
    assert {mixes.genre_id(f"Genre{n}") for n in range(6)} <= offered and mixes.genre_id("Genre6") not in offered
    assert {"decade-1960", "decade-1970", "decade-1980", "decade-1990"} <= offered and "decade-2000" not in offered
    conn.close()
