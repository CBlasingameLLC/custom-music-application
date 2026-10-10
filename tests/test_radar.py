"""The release radar: what the services say, what is worth keeping, and how a refresh behaves when things go wrong."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from musictoolkit.config import Config
from musictoolkit.db.connection import connect
from musictoolkit.integrations.listenbrainz_client import ListenBrainzError
from musictoolkit.recommend import radar

TODAY = date(2026, 10, 10)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
SINCE, UNTIL = radar.window(TODAY)

A, B, C, D = ("aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002",
              "cccccccc-0000-4000-8000-000000000003", "dddddddd-0000-4000-8000-000000000004")
ARTIST = "a74b1b7f-71a5-4011-9441-d0b5e4122711"


def lb(mbid=A, name="Tycho", title="Infinite Mile", kind="Album", day="2026-09-10", secondary=None, **extra):
    """An entry as ListenBrainz sends it."""
    return {"artist_credit_name": name, "artist_mbids": [ARTIST], "caa_id": 46149234311, "caa_release_mbid": mbid, "confidence": 12,
            "listen_count": 0, "release_date": day, "release_group_mbid": mbid, "release_group_primary_type": kind,
            "release_group_secondary_type": secondary, "release_mbid": mbid, "release_name": title, "release_tags": [], **extra}


def mb(mbid=A, title="Infinite Mile", kind="Album", day="2026-09-10", secondary=None, credit=("Tycho", ARTIST), **extra):
    """A release group as musicbrainzngs returns it."""
    group = {"id": mbid, "title": title, "first-release-date": day, "primary-type": kind, "artist-credit": [{"name": credit[0], "artist": {"id": credit[1], "name": credit[0]}}],
             "artist-credit-phrase": credit[0], "ext:score": "100", **extra}
    if secondary:
        group["secondary-type-list"] = secondary
    return group


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "library.db")
    yield connection
    connection.close()


def own(conn, artist, album, title="Song", album_artist=None):
    conn.execute("INSERT INTO tracks (file_path, artist, album_artist, album, title) VALUES (?, ?, ?, ?, ?)", (f"{artist}-{album}-{title}.mp3", artist, album_artist, album, title))
    conn.commit()


def play(conn, artist, times=1, track="Song"):
    for n in range(times):
        conn.execute("INSERT INTO play_history (source, played_at_epoch, raw_artist_name, raw_track_name) VALUES ('future_scrobble', ?, ?, ?)", (1_700_000_000 + n, artist, track))
    conn.commit()


def config(username="", contact="", enabled=True) -> Config:
    cfg = Config()
    cfg.listenbrainz.username, cfg.listenbrainz.enabled = username, enabled
    cfg.musicbrainz.contact = contact
    return cfg


# ------------------------------------------------------------------------------------------- reading ListenBrainz


def test_a_listenbrainz_entry_becomes_a_release_with_a_cover_the_service_knows_about() -> None:
    (release,) = radar.from_listenbrainz([lb(kind="EP")], SINCE, UNTIL)

    assert (release.artist, release.title, release.kind, release.date, release.source) == ("Tycho", "Infinite Mile", "EP", "2026-09-10", "listenbrainz")
    assert release.mbid == A and release.artist_mbid == ARTIST
    assert release.art_url == f"https://coverartarchive.org/release/{A}/46149234311-250.jpg"


def test_an_entry_without_a_cover_has_none() -> None:
    (release,) = radar.from_listenbrainz([lb(caa_id=None, caa_release_mbid=None)], SINCE, UNTIL)

    assert release.art_url is None


@pytest.mark.parametrize("secondary", ["Live", "Compilation", "DJ-mix", "Remix", "Soundtrack"])
def test_a_live_album_a_compilation_a_mix_a_remix_or_a_soundtrack_is_not_news(secondary) -> None:
    assert radar.from_listenbrainz([lb(secondary=secondary)], SINCE, UNTIL) == []


@pytest.mark.parametrize("kind", ["Broadcast", "Other", None])
def test_only_albums_eps_and_singles_count(kind) -> None:
    assert radar.from_listenbrainz([lb(kind=kind)], SINCE, UNTIL) == []


def test_only_the_window_counts_the_last_two_months_and_the_coming_six_weeks() -> None:
    assert (SINCE, UNTIL) == (date(2026, 8, 11), date(2026, 11, 24))
    entries = [lb(A, day="2026-08-11"), lb(B, day="2026-08-10"), lb(C, day="2026-11-24"), lb(D, day="2026-11-25")]

    assert [r.mbid for r in radar.from_listenbrainz(entries, SINCE, UNTIL)] == [A, C]


def test_a_month_alone_counts_from_its_first_day_and_a_bare_year_says_too_little() -> None:
    (release,) = radar.from_listenbrainz([lb(A, day="2026-10"), lb(B, day="2026")], SINCE, UNTIL)

    assert release.mbid == A and release.date == "2026-10"


@pytest.mark.parametrize("bad", [{"release_group_mbid": "../../x"}, {"release_group_mbid": "A" * 36}, {"release_group_mbid": None}, {"release_name": ""},
                                 {"artist_credit_name": None}, {"release_date": "soon"}, {"release_date": "2026-13-40"}])
def test_an_entry_with_a_broken_field_is_skipped_not_trusted(bad) -> None:
    assert radar.from_listenbrainz([lb(**bad)], SINCE, UNTIL) == []


def test_a_cover_with_a_strange_id_is_dropped_but_the_release_stays() -> None:
    (release,) = radar.from_listenbrainz([lb(caa_release_mbid="x/../y", caa_id="1; DROP"), ], SINCE, UNTIL)

    assert release.art_url is None


def test_a_name_far_longer_than_any_real_one_is_cut_short() -> None:
    (from_lb,) = radar.from_listenbrainz([lb(name="A" * 5000, title="T" * 5000)], SINCE, UNTIL)
    (from_mb,) = radar.from_musicbrainz("B" * 5000, [mb(title="T" * 5000, credit=("B" * 5000, ARTIST))], SINCE, UNTIL)

    assert len(from_lb.artist) == len(from_lb.title) == len(from_mb.artist) == len(from_mb.title) == radar.MAX_NAME


def test_junk_instead_of_a_list_is_nothing() -> None:
    assert radar.from_listenbrainz(None, SINCE, UNTIL) == radar.from_listenbrainz("x", SINCE, UNTIL) == radar.from_listenbrainz([None, 5, "x"], SINCE, UNTIL) == []


# ------------------------------------------------------------------------------------------- reading MusicBrainz


def test_a_musicbrainz_group_credited_to_the_artist_becomes_a_release() -> None:
    (release,) = radar.from_musicbrainz("Tycho", [mb()], SINCE, UNTIL)

    assert (release.artist, release.title, release.kind, release.source, release.art_url) == ("Tycho", "Infinite Mile", "Album", "musicbrainz", None)
    assert release.mbid == A and release.artist_mbid == ARTIST


def test_a_search_for_one_artist_does_not_take_another_who_sounds_alike() -> None:
    similar = mb(B, credit=("Tycho 2", "11111111-0000-4000-8000-000000000001"), **{"artist-credit-phrase": "Tycho 2 and Someone"})

    assert [r.mbid for r in radar.from_musicbrainz("Tycho", [similar, mb(A)], SINCE, UNTIL)] == [A]


def test_a_collaboration_counts_for_each_artist_in_it() -> None:
    group = mb(A)
    group["artist-credit"] = [{"name": "Other", "artist": {"id": B, "name": "Other"}}, " and ", {"name": "Tycho", "artist": {"id": ARTIST, "name": "Tycho"}}]

    assert [r.artist_mbid for r in radar.from_musicbrainz("Tycho", [group], SINCE, UNTIL)] == [ARTIST]


def test_names_that_differ_in_case_and_punctuation_are_the_same_artist() -> None:
    assert len(radar.from_musicbrainz("tycho!", [mb()], SINCE, UNTIL)) == 1


def test_musicbrainz_groups_get_the_same_rules_as_listenbrainz_entries() -> None:
    groups = [mb(A, secondary=["Live"]), mb(B, day="2025-01-01"), mb(C, kind="Broadcast"), mb(D, day="2026-09"), mb("../x"), {"id": A}, None]

    assert [r.mbid for r in radar.from_musicbrainz("Tycho", groups, SINCE, UNTIL)] == [D]


def test_the_search_query_asks_for_the_artist_the_dates_and_the_kinds_and_escapes_quotes() -> None:
    query = radar.query_for('Say "Hi" \\ Co', date(2026, 8, 11), date(2026, 11, 24))

    assert query == ('artist:"Say \\"Hi\\" \\\\ Co" AND firstreleasedate:[2026-08-11 TO 2026-11-24] '
                     'AND (primarytype:album OR primarytype:ep OR primarytype:single)')


# ------------------------------------------------------------------------------------------- the library and the history


def test_artists_to_ask_about_are_the_most_played_without_repeats_or_placeholders(conn) -> None:
    for artist, times in [("Tycho", 5), ("TYCHO", 3), ("Bonobo", 4), ("Various Artists", 9), ("unknown artist", 8), ("", 7)]:
        play(conn, artist, times)

    seeds = radar.seed_artists(conn, limit=10)

    assert [s.lower() for s in seeds[:2]] == ["tycho", "bonobo"] and len(seeds) == 2


def test_with_little_history_the_library_fills_in_most_common_first(conn) -> None:
    play(conn, "Played Once")
    for _ in range(3):
        own(conn, "Library Big", "A", title=f"t{_}")
    own(conn, "Library Small", "B")

    assert radar.seed_artists(conn) == ["Played Once", "Library Big", "Library Small"]


def test_the_number_of_artists_asked_about_is_capped(conn) -> None:
    for n in range(60):
        play(conn, f"Artist {n:02d}", times=60 - n)

    assert len(radar.seed_artists(conn, limit=7)) == 7


def test_what_the_library_has_is_found_by_album_or_song_for_the_artist_or_album_artist(conn) -> None:
    own(conn, "Tycho", "Dive", title="A Walk", album_artist="Tycho & Co")

    owned = radar.owned_pairs(conn)

    assert {("tycho", "dive"), ("tycho", "awalk"), ("tychoco", "dive")} <= owned


# ------------------------------------------------------------------------------------------- keeping them


def release(mbid=A, artist="Tycho", title="Infinite Mile", day="2026-09-10", source="listenbrainz", art=None):
    return radar.Release(mbid, artist, ARTIST, title, "Album", day, source, art)


def kept(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM fresh_releases ORDER BY release_date DESC, id")]


def test_new_releases_are_kept_with_how_much_the_artist_is_played(conn) -> None:
    play(conn, "Tycho", 4)

    saved = radar.save(conn, [release()], radar.plays_by_artist(conn), set(), SINCE, NOW)

    (row,) = kept(conn)
    assert saved == {"new": 1, "known": 0, "owned": 0}
    assert (row["artist"], row["title"], row["plays"], row["status"], row["first_seen"]) == ("Tycho", "Infinite Mile", 4, "new", NOW.isoformat())


def test_a_release_the_library_already_has_is_left_out(conn) -> None:
    own(conn, "Tycho", "Infinite Mile")

    saved = radar.save(conn, [release(), release(B, title="Other")], {}, radar.owned_pairs(conn), SINCE, NOW)

    assert saved == {"new": 1, "known": 0, "owned": 1} and [r["title"] for r in kept(conn)] == ["Other"]


def test_a_second_refresh_updates_what_it_knew_and_never_forgets_a_dismissal(conn) -> None:
    radar.save(conn, [release()], {}, set(), SINCE, NOW)
    conn.execute("UPDATE fresh_releases SET status = 'dismissed'")
    conn.commit()

    saved = radar.save(conn, [release(day="2026-09-12", title="Infinite Mile (new title)", art="https://x/y.jpg")], {}, set(), SINCE, NOW + timedelta(days=1))

    (row,) = kept(conn)
    assert saved == {"new": 0, "known": 1, "owned": 0}
    assert row["status"] == "dismissed" and row["release_date"] == "2026-09-12" and row["title"] == "Infinite Mile (new title)"
    assert row["first_seen"] == NOW.isoformat() and row["art_url"] == "https://x/y.jpg"


def test_a_known_cover_is_not_lost_when_a_later_source_has_none(conn) -> None:
    radar.save(conn, [release(art="https://x/y.jpg")], {}, set(), SINCE, NOW)
    radar.save(conn, [release(source="musicbrainz", art=None)], {}, set(), SINCE, NOW)

    assert kept(conn)[0]["art_url"] == "https://x/y.jpg"


def test_what_is_too_old_to_be_news_is_forgotten_dismissed_or_not(conn) -> None:
    radar.save(conn, [release(A, day="2026-08-12"), release(B, day="2026-08-01"), release(C, day="2026-10-01")], {}, set(), date(2026, 8, 5), NOW)
    conn.execute("UPDATE fresh_releases SET status = 'dismissed' WHERE release_group_mbid = ?", (B,))

    radar.save(conn, [], {}, set(), SINCE, NOW)  # SINCE is 2026-08-11

    assert sorted(r["release_group_mbid"] for r in kept(conn)) == [A, C]


def test_it_is_time_to_look_again_after_twenty_hours() -> None:
    assert radar.due(None, NOW) and radar.due("not a time", NOW)
    assert not radar.due((NOW - timedelta(hours=19)).isoformat(), NOW)
    assert radar.due((NOW - timedelta(hours=21)).isoformat(), NOW)


def test_links_look_things_up_and_download_nothing() -> None:
    links = radar.links("Tycho & Co", "Dive: Live", A)

    assert links[0] == {"label": "MusicBrainz", "url": f"https://musicbrainz.org/release-group/{A}"}
    assert [l["label"] for l in links] == ["MusicBrainz", "Bandcamp", "YouTube"]
    assert "q=Tycho+%26+Co+Dive%3A+Live" in links[1]["url"] and all(l["url"].startswith("https://") for l in links)


# ------------------------------------------------------------------------------------------- a whole refresh


class Clients:
    """Stand-ins for the two services: what they answer, and what they were asked."""

    def __init__(self, monkeypatch) -> None:
        self.lb_answer: object = []
        self.mb_answers: dict[str, object] = {}
        self.asked_lb: list[tuple] = []
        self.asked_mb: list[str] = []
        self.configured: list[tuple] = []
        monkeypatch.setattr(radar.listenbrainz_client, "get_fresh_releases", self._lb)
        monkeypatch.setattr(radar.musicbrainz_client, "search_release_groups", self._mb)
        monkeypatch.setattr(radar.musicbrainz_client, "configure", lambda *args: self.configured.append(args))

    def _lb(self, user, days, token):
        self.asked_lb.append((user, days, token))
        if isinstance(self.lb_answer, Exception):
            raise self.lb_answer
        return self.lb_answer

    def _mb(self, query, limit=25):
        artist = query.split('"')[1]
        self.asked_mb.append(artist)
        answer = self.mb_answers.get(artist, [])
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def clients(monkeypatch) -> Clients:
    return Clients(monkeypatch)


def test_listenbrainz_is_asked_first_and_its_answer_is_kept(conn, clients) -> None:
    clients.lb_answer = [lb(A), lb(B, name="Bonobo", title="Fragments")]
    own(conn, "Bonobo", "Fragments")
    messages = []

    result = radar.refresh(conn, config(username="cayl"), today=TODAY, now=NOW, progress=messages.append)

    assert clients.asked_lb == [("cayl", radar.PAST_DAYS, None)] and clients.asked_mb == []
    assert (result["source"], result["found"], result["new"], result["owned"]) == ("listenbrainz", 2, 1, 1)
    assert [r["title"] for r in kept(conn)] == ["Infinite Mile"] and messages[0].startswith("Asking ListenBrainz")
    assert radar.state(conn) == {"last_run": NOW.isoformat(), "source": "listenbrainz", "message": None}


def test_the_token_is_sent_when_there_is_one(conn, clients) -> None:
    cfg = config(username="cayl")
    cfg.listenbrainz.user_token = "tok"

    radar.refresh(conn, cfg, today=TODAY, now=NOW)

    assert clients.asked_lb[0][2] == "tok"


def test_a_listenbrainz_account_with_nothing_yet_falls_back_to_musicbrainz(conn, clients) -> None:
    play(conn, "Tycho", 3)
    play(conn, "Bonobo", 2)
    clients.mb_answers = {"Tycho": [mb(A)], "Bonobo": [mb(B, title="Fragments", credit=("Bonobo", B))]}

    result = radar.refresh(conn, config(username="cayl", contact="me@example.com"), today=TODAY, now=NOW)

    assert clients.asked_mb == ["Tycho", "Bonobo"] and clients.configured[0][2] == "me@example.com"
    assert (result["source"], result["new"], result["artists"]) == ("musicbrainz", 2, 2)
    assert "has no new releases" in result["messages"][0]
    assert sorted(r["plays"] for r in kept(conn)) == [2, 3]


def test_a_listenbrainz_failure_falls_back_and_says_so(conn, clients) -> None:
    play(conn, "Tycho")
    clients.lb_answer = ListenBrainzError("could not reach ListenBrainz (ConnectionError)")
    clients.mb_answers = {"Tycho": [mb(A)]}

    result = radar.refresh(conn, config(username="cayl", contact="me@example.com"), today=TODAY, now=NOW)

    assert result["source"] == "musicbrainz" and result["new"] == 1 and "could not reach ListenBrainz" in result["messages"][0]


def test_musicbrainz_only_when_there_is_no_listenbrainz_account(conn, clients) -> None:
    play(conn, "Tycho")
    clients.mb_answers = {"Tycho": [mb(A)]}

    result = radar.refresh(conn, config(contact="me@example.com"), today=TODAY, now=NOW)

    assert clients.asked_lb == [] and result["source"] == "musicbrainz" and result["new"] == 1


def test_listenbrainz_turned_off_in_settings_is_not_asked(conn, clients) -> None:
    play(conn, "Tycho")

    radar.refresh(conn, config(username="cayl", contact="me@example.com", enabled=False), today=TODAY, now=NOW)

    assert clients.asked_lb == [] and clients.asked_mb == ["Tycho"]


def test_with_nowhere_to_look_it_says_what_to_add_and_records_nothing(conn, clients) -> None:
    with pytest.raises(radar.RadarUnavailable, match="ListenBrainz username.*MusicBrainz contact"):
        radar.refresh(conn, config(), today=TODAY, now=NOW)

    assert radar.state(conn)["last_run"] is None


def test_a_listenbrainz_failure_with_no_other_way_is_an_error_that_will_be_tried_again(conn, clients) -> None:
    clients.lb_answer = ListenBrainzError("could not reach ListenBrainz (ConnectionError)")

    with pytest.raises(radar.RadarUnavailable, match="could not reach ListenBrainz"):
        radar.refresh(conn, config(username="cayl"), today=TODAY, now=NOW)

    assert radar.state(conn)["last_run"] is None, "nothing was learned, so it is not counted as a look"


def test_listenbrainz_with_nothing_and_no_contact_is_still_a_look_that_found_nothing(conn, clients) -> None:
    result = radar.refresh(conn, config(username="cayl"), today=TODAY, now=NOW)

    assert result["found"] == 0 and result["source"] == "listenbrainz" and radar.state(conn)["last_run"] == NOW.isoformat()
    assert any("add a contact" in m for m in result["messages"])


def test_musicbrainz_without_any_history_or_library_has_nothing_to_look_from(conn, clients) -> None:
    with pytest.raises(radar.RadarUnavailable, match="no listening history"):
        radar.refresh(conn, config(contact="me@example.com"), today=TODAY, now=NOW)


def test_musicbrainz_is_given_up_on_after_three_artists_in_a_row_fail(conn, clients) -> None:
    for n in range(6):
        play(conn, f"Artist {n}", times=10 - n)
    clients.mb_answers = {f"Artist {n}": OSError("down") for n in range(6)}

    with pytest.raises(radar.RadarUnavailable, match="did not answer"):
        radar.refresh(conn, config(contact="me@example.com"), today=TODAY, now=NOW)

    assert clients.asked_mb == ["Artist 0", "Artist 1", "Artist 2"]


def test_one_artist_that_fails_among_good_answers_does_not_stop_the_rest(conn, clients) -> None:
    for n, artist in enumerate(["Tycho", "Bonobo", "Emancipator"]):
        play(conn, artist, times=9 - n)
    clients.mb_answers = {"Tycho": [mb(A)], "Bonobo": OSError("blip"), "Emancipator": [mb(C, title="Koda", credit=("Emancipator", C))]}

    result = radar.refresh(conn, config(contact="me@example.com"), today=TODAY, now=NOW)

    assert clients.asked_mb == ["Tycho", "Bonobo", "Emancipator"] and result["new"] == 2


def test_a_cancelled_job_stops_between_artists_and_keeps_nothing_half_done(conn, clients) -> None:
    for n in range(5):
        play(conn, f"Artist {n}", times=10 - n)
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 3:
            raise KeyboardInterrupt("cancelled")  # whatever the job runner raises

    with pytest.raises(KeyboardInterrupt):
        radar.refresh(conn, config(contact="me@example.com"), today=TODAY, now=NOW, check=check)

    assert clients.asked_mb == ["Artist 0", "Artist 1"] and kept(conn) == [] and radar.state(conn)["last_run"] is None
