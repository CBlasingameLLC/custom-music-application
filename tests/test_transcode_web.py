"""A song at a lower bitrate where a listener meets it: the stream route of the web app and the stream call of a Subsonic app. ffmpeg is stood in for by
tests/fake_ffmpeg.py (a song here is a file that says what the stand-in is to do), so that a slow song, a crash and a hang can be made to happen."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from musictoolkit import transcode
from musictoolkit.config import Config
from musictoolkit.transcode import Transcoder, TranscodeError
from musictoolkit.web import streaming
from musictoolkit.web.app import create_app
from tests.fake_ffmpeg import chunk
from tests.subsonic_support import Rest

STUB = [sys.executable, str(Path(__file__).with_name("fake_ffmpeg.py"))]


def said(kbps: int, offset: str, count: int) -> bytes:
    return b"".join(chunk(str(kbps), offset, number) for number in range(count))


@pytest.fixture
def world(web: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The app, with the stand-in for ffmpeg; `add` puts a song in the library whose file says what the stand-in is to do."""
    log = tmp_path / "ffmpeg.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    web.ctx.transcoder = Transcoder(cache_dir=tmp_path / "cache", cache_mb=10, jobs=2, command=STUB)
    folder = tmp_path / "songs"
    folder.mkdir()

    def add(plan: str, *, name: str = "song.flac", format: str | None = "flac", bitrate: int | None = 900_000, duration: float | None = 240.0, no_audio: int = 0) -> int:
        path = folder / name
        path.write_text(plan, encoding="utf-8")
        with web.ctx.db() as conn:
            cursor = conn.execute(
                "INSERT INTO tracks (file_path, title, artist, album, format, bitrate, file_size, duration_seconds, no_audio, date_added) "
                "VALUES (?, ?, 'A', 'B', ?, ?, ?, ?, ?, '2026-01-01 00:00:00')",
                (str(path), name, format, bitrate, path.stat().st_size, duration, no_audio),
            )
            conn.commit()
            return cursor.lastrowid

    def runs() -> list[str]:
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    yield SimpleNamespace(web=web, client=web.client, add=add, runs=runs, folder=folder, transcoder=web.ctx.transcoder, log=log)
    web.ctx.transcoder.close()


def stream(world: SimpleNamespace, track: int, **params):
    return world.client.get(f"/api/tracks/{track}/stream", params=params)


def wait_for(world: SimpleNamespace, track: int, kbps: int) -> Path:
    """Until the song has been made and kept (the answer that was heard may have been the live one, and the file is renamed a moment after it ends)."""
    with world.web.ctx.db() as conn:
        path = Path(conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track,)).fetchone()[0])
    key = world.transcoder.key(track, path, kbps)
    deadline = time.monotonic() + 10
    while world.transcoder.cached(key) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    kept = world.transcoder.cached(key)
    assert kept is not None, "the song was not kept"
    return kept


# ------------------------------------------------------------------------------------------------------------------------------ the web app


def test_a_song_is_sent_as_it_is_when_nothing_is_asked_for_and_a_part_of_it_can_be_asked_for(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0")
    whole = stream(world, track)
    assert whole.status_code == 200 and whole.content == b"ok 5 0" and whole.headers["content-type"] == "audio/flac" and whole.headers["accept-ranges"] == "bytes"
    part = world.client.get(f"/api/tracks/{track}/stream", headers={"range": "bytes=1-2"})
    assert part.status_code == 206 and part.content == b"k " and part.headers["content-range"] == "bytes 1-2/6"
    assert stream(world, track, kbps=0).content == b"ok 5 0" and world.runs() == []


def test_a_song_over_the_bitrate_asked_for_is_converted_and_handed_out_as_it_is_made(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0")
    heard = stream(world, track, kbps=128)
    assert heard.status_code == 200 and heard.content == said(128, "0", 5)
    assert heard.headers["content-type"] == "audio/mpeg" and heard.headers["accept-ranges"] == "none" and heard.headers["cache-control"] == "no-store"
    assert "content-length" not in heard.headers  # (it is not known until the song is whole)
    assert len(world.runs()) == 1 and " -b:a 128k " in world.runs()[0]


def test_the_second_time_it_is_a_plain_file_that_can_be_asked_for_a_part_of_and_ffmpeg_is_not_run_again(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0")
    stream(world, track, kbps=128)
    kept = wait_for(world, track, 128)
    again = stream(world, track, kbps=128)
    assert again.status_code == 200 and again.content == said(128, "0", 5) == kept.read_bytes()
    assert again.headers["content-type"] == "audio/mpeg" and again.headers["accept-ranges"] == "bytes" and again.headers["content-length"] == "5000"
    assert again.headers["cache-control"] == "private, max-age=3600"
    part = world.client.get(f"/api/tracks/{track}/stream?kbps=128", headers={"range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == said(128, "0", 5)[100:200] and part.headers["content-range"] == "bytes 100-199/5000"
    assert len(world.runs()) == 1


def test_a_request_for_the_whole_from_the_first_byte_is_answered_at_once_as_a_live_stream(world: SimpleNamespace) -> None:
    track = world.add("ok 6 100")
    began = time.monotonic()
    heard = world.client.get(f"/api/tracks/{track}/stream?kbps=96", headers={"range": "bytes=0-"})
    assert heard.status_code == 200 and heard.content == said(96, "0", 6) and heard.headers["accept-ranges"] == "none"
    assert time.monotonic() - began < 10


def test_a_request_for_a_part_waits_for_the_whole_song_and_is_then_answered_from_the_file(world: SimpleNamespace) -> None:
    track = world.add("ok 8 100")
    part = world.client.get(f"/api/tracks/{track}/stream?kbps=128", headers={"range": "bytes=0-1"})  # (what Safari asks first)
    assert part.status_code == 206 and part.content == b"[1" and part.headers["content-range"] == "bytes 0-1/8000"
    assert len(world.runs()) == 1
    middle = world.client.get(f"/api/tracks/{track}/stream?kbps=128", headers={"range": "bytes=4000-"})
    assert middle.status_code == 206 and middle.content == said(128, "0", 8)[4000:] and middle.headers["content-range"] == "bytes 4000-7999/8000"


def test_a_part_of_a_song_that_could_not_be_made_is_sent_from_the_original(world: SimpleNamespace) -> None:
    track = world.add("crash 2 0")
    part = world.client.get(f"/api/tracks/{track}/stream?kbps=128", headers={"range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == b"cras" and part.headers["content-type"] == "audio/flac"


def test_asking_what_the_answer_would_be_starts_nothing(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0")
    head = world.client.head(f"/api/tracks/{track}/stream?kbps=128")
    assert head.status_code == 200 and head.headers["content-type"] == "audio/mpeg" and head.headers["accept-ranges"] == "none" and head.content == b""
    time.sleep(0.3)
    assert world.runs() == [] and not list((world.web.ctx.data_dir / "cache").glob("**/*.partial"))
    stream(world, track, kbps=128)
    wait_for(world, track, 128)
    kept = world.client.head(f"/api/tracks/{track}/stream?kbps=128")
    assert kept.headers["content-length"] == "5000" and kept.headers["accept-ranges"] == "bytes" and kept.content == b""


def test_a_song_that_is_small_enough_is_sent_as_it_is_and_nothing_is_run(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0", name="small.mp3", format="mp3", bitrate=96_000)
    heard = stream(world, track, kbps=128)
    assert heard.content == b"ok 5 0" and heard.headers["content-type"] == "audio/mpeg" and world.runs() == []


def test_raw_is_the_file_whatever_the_bitrate_and_mp3_is_the_song_at_the_usual_bitrate(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0")
    assert stream(world, track, kbps=64, format="raw").content == b"ok 5 0" and world.runs() == []
    assert stream(world, track, format="mp3").content == said(192, "0", 5)
    assert stream(world, track, format="opus").content == b"ok 5 0"  # (a format that is not made is not asked for)


def test_a_song_that_cannot_be_made_is_sent_as_it_is(world: SimpleNamespace) -> None:
    for plan in ("crash 0 0", "empty"):  # (it made nothing: no byte has gone out, so the answer is still the file)
        track = world.add(plan, name=f"{plan.replace(' ', '-')}.flac")
        heard = stream(world, track, kbps=128)
        assert heard.status_code == 200 and heard.content == plan.encode() and heard.headers["content-type"] == "audio/flac"


def test_a_song_that_stopped_in_the_middle_cuts_that_listener_and_the_next_one_is_sent_the_file(world: SimpleNamespace) -> None:
    track = world.add("crash 3 0")
    first = stream(world, track, kbps=128)  # (3,000 bytes had gone out: there is no way to say it in what was begun)
    assert first.headers["content-type"] == "audio/mpeg" and first.content == said(128, "0", 3)
    again = stream(world, track, kbps=128)
    assert again.content == b"crash 3 0" and again.headers["content-type"] == "audio/flac"
    assert len(world.runs()) == 1  # (it was not tried again)
    assert stream(world, track, kbps=96).headers["content-type"] == "audio/mpeg"  # (another bitrate is another song)


def test_asking_what_the_answer_would_be_for_a_song_that_failed_is_told_the_file(world: SimpleNamespace) -> None:
    track = world.add("crash 3 0")
    stream(world, track, kbps=128)
    head = world.client.head(f"/api/tracks/{track}/stream?kbps=128")
    assert head.status_code == 200 and head.headers["content-type"] == "audio/flac" and head.headers["content-length"] == str(len("crash 3 0"))


def test_without_ffmpeg_a_song_is_sent_as_it_is(world: SimpleNamespace, tmp_path: Path) -> None:
    world.web.ctx.transcoder = Transcoder(ffmpeg=str(tmp_path / "no-ffmpeg"))
    track = world.add("ok 5 0")
    assert stream(world, track, kbps=128).content == b"ok 5 0" and world.runs() == []


def test_when_every_place_for_converting_is_taken_a_song_is_sent_as_it_is(world: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcode, "SLOT_WAIT", 0.2)
    world.web.ctx.transcoder = Transcoder(cache_dir=tmp_path / "cache2", jobs=1, command=STUB)
    slow, other = world.add("ok 10 150", name="slow.flac"), world.add("ok 2 0", name="other.flac")
    key = world.web.ctx.transcoder.key(slow, world.folder / "slow.flac", 128)
    holding = world.web.ctx.transcoder.job(key, world.folder / "slow.flac", 128)
    assert holding.started.wait(10)
    assert stream(world, other, kbps=128).content == b"ok 2 0"
    holding.cancelled = True
    world.web.ctx.transcoder.close()


def test_two_listeners_of_a_song_that_is_being_made_hear_the_same_and_ffmpeg_is_run_once(world: SimpleNamespace) -> None:
    track = world.add("ok 10 60")
    heard: list[bytes] = []
    clients = [TestClient(world.web.app) for _ in range(2)]
    threads = [threading.Thread(target=lambda c=c: heard.append(c.get(f"/api/tracks/{track}/stream?kbps=128").content)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert heard == [said(128, "0", 10)] * 2 and len(world.runs()) == 1


def test_a_song_that_is_only_tags_is_refused_whatever_is_asked(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0", no_audio=1)
    for params in ({}, {"kbps": 128}):
        assert stream(world, track, **params).status_code == 422
    assert world.runs() == []


def test_a_song_that_is_not_there_and_a_bitrate_that_is_not_one_are_refused(world: SimpleNamespace) -> None:
    assert stream(world, 999_999, kbps=128).status_code == 404
    track = world.add("ok 5 0")
    for bad in ({"kbps": -1}, {"kbps": 100_001}, {"kbps": "many"}):
        assert stream(world, track, **bad).status_code == 422
    assert stream(world, track, kbps=100_000).content == b"ok 5 0"  # (nothing is over that)


def test_a_song_whose_file_has_gone_is_not_found_and_not_converted(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0")
    (world.folder / "song.flac").unlink()
    assert stream(world, track, kbps=128).status_code == 404 and world.runs() == []


def test_about_says_whether_songs_can_be_converted_and_with_what(world: SimpleNamespace, tmp_path: Path) -> None:
    assert world.client.get("/api/about").json()["transcoding"] == {"available": True, "ffmpeg": "stub-1.0", "reason": None}
    world.web.ctx.transcoder = Transcoder(ffmpeg=str(tmp_path / "no-ffmpeg"))
    assert world.client.get("/api/about").json()["transcoding"] == {"available": False, "ffmpeg": None, "reason": "ffmpeg was not found (on Linux: sudo apt install ffmpeg)"}


def made_app(tmp_path: Path, **server: object):
    config = Config()
    config.database.path = str(tmp_path / "data" / "library.db")
    config.logging.dir = str(tmp_path / "logs")
    for name, value in server.items():
        setattr(config.server, name, value)
    return create_app(db_path=Path(config.database.path), config_path=tmp_path / "config.toml", config=config)


def test_the_app_makes_its_transcoder_as_the_config_and_the_data_folder_say_and_closes_it_when_it_stops(tmp_path: Path) -> None:
    app = made_app(tmp_path, ffmpeg=f"  {tmp_path / 'my-ffmpeg'}  ", transcode_cache_mb=7, transcode_jobs=3)
    made = app.state.ctx.transcoder
    assert isinstance(made, Transcoder) and made._ffmpeg == str(tmp_path / "my-ffmpeg") and made._limit == 7 * 1024 * 1024
    assert made._dir == tmp_path / "cache" / "transcode" and made._slots._value == 3
    with TestClient(app):
        assert made._closed is False
    assert made._closed is True


def test_the_app_clears_the_cache_when_it_starts(tmp_path: Path) -> None:
    app = made_app(tmp_path)
    cache = tmp_path / "cache" / "transcode"
    cache.mkdir(parents=True)
    old, fresh = cache / "1-1-1-128.mp3.partial", cache / "2-1-1-128.mp3.partial"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    then = time.time() - transcode.STALE_PARTIAL - 10
    os.utime(old, (then, then))
    with TestClient(app):
        assert not old.exists() and fresh.exists()


def test_a_wrong_setting_is_said_in_the_log_and_the_usual_ones_are_used_by_the_app(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="musictoolkit"):
        app = made_app(tmp_path, transcode_jobs=0)
    assert [r.getMessage() for r in caplog.records if "transcode_jobs" in r.getMessage()] == ["[server] transcode_jobs must be a whole number from 1 to 16. The usual settings are used."]
    made = app.state.ctx.transcoder
    assert made._slots._value == transcode.DEFAULT_JOBS and made._limit == transcode.DEFAULT_CACHE_MB * 1024 * 1024 and made._ffmpeg == ""


# ------------------------------------------------------------------------------------------------------------------------------ a Subsonic app


def call(world: SimpleNamespace, name: str, **params):
    return Rest(world.client).get(name, **params)


def test_a_subsonic_app_that_asks_for_a_bitrate_is_sent_the_song_at_it(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0")
    answer = call(world, "stream", id=track, maxBitRate=128)
    assert answer.status_code == 200 and answer.content == said(128, "0", 5) and answer.headers["content-type"] == "audio/mpeg"
    assert call(world, "stream", id=track, maxBitRate=0).content == b"ok 5 0"
    assert call(world, "stream", id=track).content == b"ok 5 0"
    assert call(world, "stream", id=track, maxBitRate=-5).content == b"ok 5 0"
    assert call(world, "stream", id=track, maxBitRate=64, format="raw").content == b"ok 5 0"
    assert len(world.runs()) == 1


def test_a_subsonic_app_that_asks_for_mp3_is_sent_mp3_at_the_usual_bitrate_unless_it_is_mp3(world: SimpleNamespace) -> None:
    flac, mp3 = world.add("ok 4 0"), world.add("ok 4 0", name="a.mp3", format="mp3", bitrate=320_000)
    assert call(world, "stream", id=flac, format="mp3").content == said(192, "0", 4)
    assert call(world, "stream", id=mp3, format="mp3").content == b"ok 4 0"
    assert call(world, "stream", id=mp3, format="mp3", maxBitRate=96).content == said(96, "0", 4)


def test_a_download_is_the_file_whatever_is_asked(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0")
    for extra in ({}, {"maxBitRate": 64}, {"format": "mp3"}):
        got = call(world, "download", id=track, **extra)
        assert got.status_code == 200 and got.content == b"ok 5 0" and got.headers["content-type"] == "audio/flac"
    assert world.runs() == []


def test_a_subsonic_app_can_ask_for_a_song_from_a_time_and_it_is_made_for_it_alone(world: SimpleNamespace) -> None:
    track = world.add("ok 4 0")
    answer = call(world, "stream", id=track, maxBitRate=128, timeOffset=30)
    assert answer.status_code == 200 and answer.content == said(128, "30.000", 4) and answer.headers["accept-ranges"] == "none"
    assert "-ss 30.000" in world.runs()[0] and not list((world.web.ctx.data_dir / "cache").glob("**/*.mp3"))
    assert call(world, "stream", id=track, maxBitRate=128, timeOffset=0).content == said(128, "0", 4)
    assert call(world, "stream", id=track, maxBitRate=128, timeOffset=-4).content == said(128, "0", 4)
    assert call(world, "stream", id=track, timeOffset=30).content == b"ok 4 0"  # (a file as it is is sought with a Range request)


def test_a_time_offset_asked_of_a_song_that_cannot_be_made_is_sent_from_the_file(world: SimpleNamespace, tmp_path: Path) -> None:
    track = world.add("crash 0 0")  # (it made nothing: the answer is still the file)
    assert call(world, "stream", id=track, maxBitRate=128, timeOffset=30).content == b"crash 0 0"
    world.web.ctx.transcoder = Transcoder(ffmpeg=str(tmp_path / "no-ffmpeg"))
    assert call(world, "stream", id=track, maxBitRate=128, timeOffset=30).content == b"crash 0 0"


def test_asking_what_a_song_from_a_time_would_be_starts_nothing(world: SimpleNamespace) -> None:
    track = world.add("ok 4 0")
    head = world.client.head("/rest/stream", params=Rest(world.client).params(id=track, maxBitRate=128, timeOffset=30))
    assert head.status_code == 200 and head.headers["content-type"] == "audio/mpeg" and head.content == b"" and world.runs() == []


def test_a_length_that_was_asked_to_be_guessed_is_given_and_kept_to(world: SimpleNamespace) -> None:
    short = world.add("ok 5 0", name="short.flac", duration=0.04)  # (0.04 s at 128 kbps is 640 bytes: the 5,000 that are made are cut)
    got = call(world, "stream", id=short, maxBitRate=128, estimateContentLength="true")
    assert got.headers["content-length"] == "640" and got.content == said(128, "0", 5)[:640] and got.headers["content-type"] == "audio/mpeg"
    long = world.add("ok 1 0", name="long.flac", duration=0.2)  # (3,200 bytes: the 1,000 that are made are filled)
    got = call(world, "stream", id=long, maxBitRate=128, estimateContentLength="true")
    assert got.headers["content-length"] == "3200" and got.content == said(128, "0", 1) + bytes(2200)
    plain = call(world, "stream", id=long, maxBitRate=96, estimateContentLength="false")
    assert "content-length" not in plain.headers
    unknown = world.add("ok 1 0", name="unknown.flac", duration=None)
    assert "content-length" not in call(world, "stream", id=unknown, maxBitRate=128, estimateContentLength="true").headers


def test_the_length_of_a_song_from_a_time_is_what_is_left_of_it(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0", duration=0.14)  # (from 0.1 s: 0.04 s left, 640 bytes)
    got = call(world, "stream", id=track, maxBitRate=128, timeOffset=0.1, estimateContentLength="true")
    assert got.headers["content-length"] == "640" and len(got.content) == 640


def test_a_subsonic_app_that_sends_what_is_not_a_number_is_told_so(world: SimpleNamespace) -> None:
    track = world.add("ok 5 0")
    for bad, said_so in (({"maxBitRate": "lots"}, "maxBitRate is not a whole number"), ({"timeOffset": "soon"}, "timeOffset is not a number"),
                         ({"timeOffset": "nan"}, "timeOffset is not a number"), ({"timeOffset": "inf"}, "timeOffset is not a number")):
        answer = call(world, "stream", id=track, f="json", **bad)
        assert answer.status_code == 200 and answer.json()["subsonic-response"]["status"] == "failed"
        assert answer.json()["subsonic-response"]["error"] == {"code": 0, "message": said_so}
    assert world.runs() == []


def test_a_subsonic_app_that_asks_for_a_song_that_is_not_there_or_has_no_audio_is_told_so_whatever_it_asks(world: SimpleNamespace) -> None:
    only_tags = world.add("ok 5 0", name="tags.flac", no_audio=1)
    for number, code in ((999_999, 70), (only_tags, 70)):
        answer = call(world, "stream", id=number, f="json", maxBitRate=128)
        assert answer.json()["subsonic-response"]["status"] == "failed" and answer.json()["subsonic-response"]["error"]["code"] == code
    assert world.runs() == []


# --------------------------------------------------------------------------------------------------------------------------------------- mtk serve


def config_with(tmp_path: Path, server: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[server]\n{server}\n[database]\npath = "./data/library.db"\n[logging]\ndir = "./logs"\n', encoding="utf-8")
    return path


@pytest.fixture
def not_listening(monkeypatch: pytest.MonkeyPatch) -> dict:
    """`mtk serve` without really listening: whether it got as far as asking uvicorn to run."""
    import uvicorn

    seen: dict = {"run": None}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: seen.update(run=options))
    monkeypatch.delenv("MTK_TOKEN", raising=False)
    return seen


@pytest.mark.parametrize("line,said", [
    ("ffmpeg = 3", '[server] ffmpeg must be the path of the program in quotes, or "" to look for it.'),
    ("transcode_cache_mb = -5", "[server] transcode_cache_mb must be a whole number of megabytes, 0 or more."),
    ('transcode_cache_mb = "a lot"', "[server] transcode_cache_mb must be a whole number of megabytes, 0 or more."),
    ("transcode_jobs = 0", "[server] transcode_jobs must be a whole number from 1 to 16."),
    ("transcode_jobs = 99", "[server] transcode_jobs must be a whole number from 1 to 16."),
])
def test_serve_refuses_to_start_with_a_setting_for_converting_songs_that_is_wrong_and_says_which(tmp_path: Path, not_listening: dict, line: str, said: str) -> None:
    from typer.testing import CliRunner

    from musictoolkit.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["--config", str(config_with(tmp_path, line)), "serve", "--host", "127.0.0.1"])
    assert result.exit_code == 2 and f"Cannot start the server: {said}" in result.output
    assert not_listening["run"] is None and not (tmp_path / "server.token").exists()


def test_serve_starts_with_settings_for_converting_songs_that_are_right_and_with_none(tmp_path: Path, not_listening: dict) -> None:
    from typer.testing import CliRunner

    from musictoolkit.cli import app as cli_app

    for server in ("", 'ffmpeg = "/usr/bin/ffmpeg"\ntranscode_cache_mb = 512\ntranscode_jobs = 4'):
        result = CliRunner().invoke(cli_app, ["--config", str(config_with(tmp_path, server)), "serve", "--host", "127.0.0.1"])
        assert result.exit_code == 0, result.output
        assert not_listening["run"] is not None
        not_listening["run"] = None


def test_the_settings_are_read_from_the_server_section_with_the_usual_ones_when_there_are_none(tmp_path: Path) -> None:
    from musictoolkit.config import Config, load_config, save_config

    given = load_config(config_with(tmp_path, 'ffmpeg = "/opt/ffmpeg/bin/ffmpeg"\ntranscode_cache_mb = 64\ntranscode_jobs = 3')).server
    assert (given.ffmpeg, given.transcode_cache_mb, given.transcode_jobs) == ("/opt/ffmpeg/bin/ffmpeg", 64, 3)
    other = tmp_path / "other"
    other.mkdir()
    usual = load_config(config_with(other)).server
    assert (usual.ffmpeg, usual.transcode_cache_mb, usual.transcode_jobs) == ("", 2048, 2)
    save_config(tmp_path / "saved.toml", Config())
    assert load_config(tmp_path / "saved.toml").server.transcode_jobs == 2


# ------------------------------------------------------------------------------------------------------------------------- the corners the first tests left open


def test_a_song_that_is_not_there_is_not_found_and_says_so(world: SimpleNamespace) -> None:
    got = stream(world, 99_999)
    assert got.status_code == 404 and got.json() == {"detail": "track not found"}
    assert stream(world, 99_999, kbps=128).json() == {"detail": "track not found"}


def test_a_format_of_twelve_characters_is_taken_and_one_of_thirteen_is_not(world: SimpleNamespace) -> None:
    track = world.add("ok 1 0")
    assert stream(world, track, format="a" * 12).status_code == 200
    assert stream(world, track, format="a" * 13).status_code == 422


def test_the_song_as_it_is_is_kept_for_an_hour_and_a_file_of_a_kind_that_is_not_known_has_the_kind_that_says_nothing(world: SimpleNamespace) -> None:
    track = world.add("ok 1 0", name="song.xyz", format="xyz")
    got = stream(world, track)
    assert got.status_code == 200 and got.headers["content-type"] == "application/octet-stream" and got.headers["cache-control"] == "private, max-age=3600"
    flac = world.add("ok 1 0")
    assert stream(world, flac).headers["content-type"] == "audio/flac"


@pytest.mark.parametrize("trouble", [TranscodeError("boom"), OSError("the drive went")])
def test_a_song_that_cannot_be_converted_is_sent_as_it_is_and_the_log_says_why(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, trouble: Exception) -> None:
    caplog.set_level(logging.WARNING, logger="musictoolkit")
    track = world.add("ok 2 0")

    def broken(*_args, **_kwargs):
        raise trouble

    monkeypatch.setattr(streaming, "converted", broken)
    got = stream(world, track, kbps=128)
    assert got.status_code == 200 and got.content == b"ok 2 0" and got.headers["content-type"] == "audio/flac"
    assert f"Song {track} is sent as it is: it could not be converted ({trouble})" in [r.getMessage() for r in caplog.records]


def test_a_cache_that_cannot_be_cleaned_when_the_app_starts_is_said_in_the_log_and_the_app_starts(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="musictoolkit")

    def broken(self, protect=None):
        raise OSError("disk gone")

    monkeypatch.setattr(Transcoder, "prune", broken)
    with TestClient(world.web.app) as client:
        assert client.get("/api/about").status_code == 200
    assert "Clearing the cache of converted songs failed: disk gone" in [r.getMessage() for r in caplog.records]


def test_what_about_says_of_converting_is_what_the_maker_says(world: SimpleNamespace) -> None:
    assert world.client.get("/api/about").json()["transcoding"] == world.transcoder.status()
