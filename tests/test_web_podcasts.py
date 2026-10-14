"""/api/podcasts: following shows, new episodes, where you stopped, and listening through the app, against a fake podcast host."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from musictoolkit.net import guard
from musictoolkit.podcasts import directory
from musictoolkit.web.podcast_job import PodcastSchedule
from tests.fake_streams import PodcastSite, serve_script

AUDIO = bytes(range(256)) * 400  # 102,400 bytes standing in for an episode (passed on, never decoded)


@pytest.fixture
def site():
    """A podcast host on this computer (addresses on this computer are allowed for the length of the test)."""
    host = PodcastSite(AUDIO)
    with guard.local_urls_allowed(), serve_script(host.script()) as server:
        host.base = server.url
        host.server = server
        yield host


def follow(web, site, path: str = "/feed.xml") -> dict:
    response = web.client.post("/api/podcasts", json={"feed_url": site.base + path})
    assert response.status_code == 201, response.text
    return response.json()


def episode_ids(web, podcast_id: int) -> dict[str, int]:
    return {e["title"]: e["id"] for e in web.client.get(f"/api/podcasts/{podcast_id}").json()["items"]}


# ---------------------------------------------------------------- following

def test_a_podcast_is_followed_from_its_feed_and_its_episodes_are_kept_unheard(web, site) -> None:
    podcast = follow(web, site)
    assert (podcast["title"], podcast["author"], podcast["description"], podcast["episodes"], podcast["unplayed"]) == ("The Test Show", "Jo Host", "About the show", 2, 2)
    assert podcast["has_art"] is True and podcast["feed_url"] == site.base + "/feed.xml" and podcast["latest"] == "2025-06-03T10:00:00+00:00"
    listing = web.client.get("/api/podcasts").json()
    assert [p["title"] for p in listing["items"]] == ["The Test Show"] and listing["automatic"] is True and listing["refreshing"] is False
    detail = web.client.get(f"/api/podcasts/{podcast['id']}").json()
    assert [e["title"] for e in detail["items"]] == ["Second", "First"] and detail["total"] == 2
    second = detail["items"][0]
    assert second["kind"] == "episode" and second["duration"] == 600 and second["played"] is False and second["position"] == 0
    assert second["podcast_title"] == "The Test Show" and second["has_art"] is True and detail["items"][1]["duration"] == 3600


def test_following_the_same_podcast_again_does_not_double_it(web, site) -> None:
    first = follow(web, site)
    again = follow(web, site)
    assert again["id"] == first["id"] and again["episodes"] == 2 and len(web.client.get("/api/podcasts").json()["items"]) == 1


@pytest.mark.parametrize(
    "path, message",
    [
        ("/page.html", "no RSS channel"),
        ("/nothing-here", "error 404"),
    ],
)
def test_an_address_that_is_not_a_podcast_is_not_followed_and_says_why(web, site, path, message) -> None:
    response = web.client.post("/api/podcasts", json={"feed_url": site.base + path})
    assert response.status_code == 422 and message in response.json()["detail"]
    assert web.client.get("/api/podcasts").json()["items"] == []


@pytest.mark.parametrize("url", ["ftp://example.com/feed", "file:///etc/passwd", "javascript:alert(1)", "http://user:pw@example.com/feed", "not a web address", "http://"])
def test_addresses_that_are_not_web_addresses_are_refused(web, url) -> None:
    assert web.client.post("/api/podcasts", json={"feed_url": url}).status_code == 422


def test_a_feed_with_entities_is_refused(web) -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>'

    def serve(h):
        h.send_response(200)
        h.send_header("Content-Length", str(len(bomb)))
        h.end_headers()
        h.wfile.write(bomb)

    with guard.local_urls_allowed(), serve_script(serve) as server:
        response = web.client.post("/api/podcasts", json={"feed_url": server.url + "/feed"})
    assert response.status_code == 422 and "not accepted" in response.json()["detail"]


def test_a_feed_on_this_computer_is_refused_when_it_is_not_a_test(web, site) -> None:
    guard.allow_local(False)  # the app as it really runs
    try:
        response = web.client.post("/api/podcasts", json={"feed_url": site.base + "/feed.xml"})
    finally:
        guard.allow_local(True)
    assert response.status_code == 422 and "not on the internet" in response.json()["detail"]
    assert site.server.requests == []


def test_a_podcast_can_be_unfollowed_and_its_episodes_go_with_it(web, site) -> None:
    podcast = follow(web, site)
    ids = episode_ids(web, podcast["id"])
    assert web.client.delete(f"/api/podcasts/{podcast['id']}").json() == {"ok": True}
    assert web.client.get(f"/api/podcasts/{podcast['id']}").status_code == 404
    assert web.client.get(f"/api/podcasts/episodes/{ids['First']}").status_code == 404
    assert web.client.delete(f"/api/podcasts/{podcast['id']}").status_code == 404


# ---------------------------------------------------------------- checking for new episodes

def test_a_new_episode_is_found_and_an_unchanged_feed_is_not_read_again(web, site) -> None:
    podcast = follow(web, site)
    ids = episode_ids(web, podcast["id"])
    web.client.post(f"/api/podcasts/episodes/{ids['First']}/progress", json={"position": 120})

    job = web.client.post(f"/api/podcasts/{podcast['id']}/refresh").json()["job"]
    done = web.ctx.jobs.wait(job["id"], timeout=30)
    assert done.status == "done" and done.result["new"] == 0 and done.result["errors"] == 0
    assert site.server.requests[-1]["headers"]["If-None-Match"] == '"v1"', "asked whether it had changed"
    assert web.client.get(f"/api/podcasts/{podcast['id']}").json()["error"] is None

    site.etag = '"v2"'
    site.episodes.insert(0, {"guid": "ep-3", "title": "Third", "path": "/ep1.mp3", "date": "Tue, 10 Jun 2025 10:00:00 +0000", "duration": "0:05:00"})
    done = web.ctx.jobs.wait(web.client.post("/api/podcasts/refresh").json()["job"]["id"], timeout=30)
    assert done.result["new"] == 1 and done.result["checked"] == 1
    detail = web.client.get(f"/api/podcasts/{podcast['id']}").json()
    assert [e["title"] for e in detail["items"]] == ["Third", "Second", "First"] and detail["unplayed"] == 3
    first = next(e for e in detail["items"] if e["title"] == "First")
    assert first["position"] == 120, "where listening stopped survives a refresh"


def test_a_feed_that_goes_wrong_is_remembered_and_forgiven_when_it_recovers(web, site) -> None:
    podcast = follow(web, site)
    site.feed_status = 503
    done = web.ctx.jobs.wait(web.client.post("/api/podcasts/refresh").json()["job"]["id"], timeout=30)
    assert done.result["errors"] == 1 and "error 503" in done.result["messages"][0]
    listed = web.client.get("/api/podcasts").json()["items"][0]
    assert "error 503" in listed["error"] and listed["episodes"] == 2, "the episodes already known stay"
    site.feed_status = 200
    site.etag = '"v3"'
    web.ctx.jobs.wait(web.client.post("/api/podcasts/refresh").json()["job"]["id"], timeout=30)
    assert web.client.get(f"/api/podcasts/{podcast['id']}").json()["error"] is None


def test_only_one_check_runs_at_a_time(web, site) -> None:
    follow(web, site)
    web.ctx.jobs.submit("podcasts", "Holding the lane", lambda handle: time.sleep(0.5), lane="network")
    assert web.client.post("/api/podcasts/refresh").status_code == 409


def test_checking_one_podcast_leaves_the_others_alone(web, site) -> None:
    first = follow(web, site)
    other = PodcastSite(AUDIO)
    other.title = "Another Show"
    with serve_script(other.script()) as server:
        other.base = server.url
        follow(web, other)
        asked = len(server.requests)
        done = web.ctx.jobs.wait(web.client.post(f"/api/podcasts/{first['id']}/refresh").json()["job"]["id"], timeout=30)
        assert done.result["checked"] == 1 and len(server.requests) == asked
        everything = web.ctx.jobs.wait(web.client.post("/api/podcasts/refresh").json()["job"]["id"], timeout=30)
        assert everything.result["checked"] == 2 and len(server.requests) == asked + 1


def test_the_automatic_check_waits_until_it_is_time_and_only_when_asked_for(web, site) -> None:
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    schedule = PodcastSchedule(web.ctx, clock=lambda: now)
    assert schedule.tick() is False, "nothing is followed"
    podcast = follow(web, site)
    with web.ctx.db() as conn:
        def checked_at(moment: datetime | None) -> None:
            conn.execute("UPDATE podcasts SET last_checked_at = ? WHERE id = ?", (moment.isoformat() if moment else None, podcast["id"]))
            conn.commit()

        checked_at(now - timedelta(hours=1))
        assert schedule.tick() is False, "looked an hour ago"
        checked_at(now - timedelta(hours=7))
        web.ctx.config.app.podcast_refresh = False
        assert schedule.tick() is False, "switched off in Settings"
        web.ctx.config.app.podcast_refresh = True
        assert schedule.tick() is True, "seven hours is long enough"
        web.ctx.jobs.wait(next(j for j in web.ctx.jobs.list() if j.kind == "podcasts" and j.status != "done").id, timeout=30)
        checked_at(None)
        assert schedule.tick() is True, "never looked at"


def test_a_podcast_never_checked_makes_it_time_even_when_the_others_were_checked_lately(web, site) -> None:
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    schedule = PodcastSchedule(web.ctx, clock=lambda: now)
    first = follow(web, site)
    other = PodcastSite(AUDIO)
    other.title = "Another Show"
    with serve_script(other.script()) as server:
        other.base = server.url
        second = follow(web, other)
        with web.ctx.db() as conn:
            conn.execute("UPDATE podcasts SET last_checked_at = ? WHERE id = ?", ((now - timedelta(minutes=5)).isoformat(), first["id"]))
            conn.execute("UPDATE podcasts SET last_checked_at = NULL WHERE id = ?", (second["id"],))
            conn.commit()
        assert schedule.tick() is True
        web.ctx.jobs.wait(next(j for j in web.ctx.jobs.list() if j.kind == "podcasts").id, timeout=30)


def test_the_automatic_check_can_be_switched_off_in_settings(web) -> None:
    assert web.client.get("/api/settings").json()["app"]["podcast_refresh"] is True
    assert web.client.put("/api/settings", json={"app": {"podcast_refresh": False}}).json()["app"]["podcast_refresh"] is False
    assert web.client.put("/api/settings", json={"app": {"podcast_refresh": "yes"}}).status_code == 422


# ---------------------------------------------------------------- episodes and where you stopped

def test_new_episodes_come_from_every_podcast_and_leave_the_list_when_heard(web, site) -> None:
    podcast = follow(web, site)
    other = PodcastSite(AUDIO)
    other.title = "Another Show"
    other.episodes = [{"guid": "x-1", "title": "Other one", "path": "/ep1.mp3", "date": "Tue, 10 Jun 2025 10:00:00 +0000", "duration": "60"}]
    with serve_script(other.script()) as server:
        other.base = server.url
        follow(web, other)
    new = web.client.get("/api/podcasts/new").json()
    assert [e["title"] for e in new["items"]] == ["Other one", "Second", "First"] and new["total"] == 3
    assert new["items"][0]["podcast_title"] == "Another Show"
    ids = episode_ids(web, podcast["id"])
    web.client.post(f"/api/podcasts/episodes/{ids['Second']}/played", json={"played": True})
    assert [e["title"] for e in web.client.get("/api/podcasts/new").json()["items"]] == ["Other one", "First"]
    assert [e["title"] for e in web.client.get("/api/podcasts/new", params={"limit": 1, "offset": 1}).json()["items"]] == ["First"]
    assert web.client.get("/api/podcasts").json()["items"][0]["unplayed"] >= 1


def test_where_listening_stopped_is_remembered_and_the_end_counts_as_heard(web, site) -> None:
    podcast = follow(web, site)
    ids = episode_ids(web, podcast["id"])
    second = ids["Second"]  # 600 seconds long
    got = web.client.post(f"/api/podcasts/episodes/{second}/progress", json={"position": 123.5}).json()
    assert got["position"] == 123.5 and got["played"] is False
    assert web.client.get(f"/api/podcasts/episodes/{second}").json()["position"] == 123.5
    near_the_end = web.client.post(f"/api/podcasts/episodes/{second}/progress", json={"position": 575}).json()
    assert near_the_end["played"] is True and near_the_end["position"] == 0 and near_the_end["played_at"], "within 30 s of the end: heard, and it starts over next time"
    long_one = ids["First"]  # 3600 seconds long: 97% of it is still 100 s before the end
    assert web.client.post(f"/api/podcasts/episodes/{long_one}/progress", json={"position": 3400}).json()["played"] is False
    assert web.client.post(f"/api/podcasts/episodes/{long_one}/progress", json={"position": 3500}).json()["played"] is True


def test_a_length_reported_by_the_page_stands_in_for_an_unknown_one(web, site) -> None:
    site.episodes = [{"guid": "g", "title": "No length", "path": "/ep1.mp3", "date": "Tue, 10 Jun 2025 10:00:00 +0000", "duration": "soon"}]
    podcast = follow(web, site)
    episode = web.client.get(f"/api/podcasts/{podcast['id']}").json()["items"][0]
    assert episode["duration"] is None
    got = web.client.post(f"/api/podcasts/episodes/{episode['id']}/progress", json={"position": 50, "duration": 1800}).json()
    assert got["duration"] == 1800 and got["played"] is False
    assert web.client.post(f"/api/podcasts/episodes/{episode['id']}/progress", json={"position": 1790}).json()["played"] is True


def test_the_length_the_page_reports_decides_when_an_episode_is_over(web, site) -> None:
    second = episode_ids(web, follow(web, site)["id"])["Second"]  # the feed says 600 s; the file the page is playing turns out longer
    got = web.client.post(f"/api/podcasts/episodes/{second}/progress", json={"position": 700, "duration": 1800}).json()
    assert got["played"] is False and got["position"] == 700, "past the length the feed gave, but nowhere near the end of the real file"


@pytest.mark.parametrize("body", [{"position": -1}, {"position": "later"}, {}, {"position": 5, "duration": -3}, {"position": 99999999}])
def test_nonsense_progress_is_refused(web, site, body) -> None:
    podcast = follow(web, site)
    episode = web.client.get(f"/api/podcasts/{podcast['id']}").json()["items"][0]
    assert web.client.post(f"/api/podcasts/episodes/{episode['id']}/progress", json=body).status_code == 422
    assert web.client.get(f"/api/podcasts/episodes/{episode['id']}").json()["position"] == 0


def test_an_episode_can_be_marked_heard_unheard_and_a_whole_podcast_heard(web, site) -> None:
    podcast = follow(web, site)
    ids = episode_ids(web, podcast["id"])
    assert web.client.post(f"/api/podcasts/episodes/{ids['First']}/played", json={"played": True}).json()["played"] is True
    assert web.client.post(f"/api/podcasts/episodes/{ids['First']}/played", json={"played": False}).json()["played"] is False
    web.client.post(f"/api/podcasts/episodes/{ids['Second']}/played", json={"played": True})
    assert web.client.post(f"/api/podcasts/{podcast['id']}/played").json() == {"marked": 1}, "only the one not heard yet is counted"
    heard = web.client.get(f"/api/podcasts/{podcast['id']}", params={"unplayed": True}).json()
    assert heard["items"] == [] and heard["total"] == 0 and heard["unplayed"] == 0
    assert web.client.post("/api/podcasts/episodes/999/played", json={"played": True}).status_code == 404
    assert web.client.post("/api/podcasts/episodes/999/progress", json={"position": 1}).status_code == 404
    assert web.client.post("/api/podcasts/999/played").status_code == 404


def test_the_list_of_episodes_is_paged(web, site) -> None:
    site.episodes = [{"guid": f"g{n}", "title": f"Episode {n:02d}", "path": "/ep1.mp3", "date": f"Tue, {n:02d} Jun 2025 10:00:00 +0000", "duration": "60"} for n in range(1, 8)]
    podcast = follow(web, site)
    page = web.client.get(f"/api/podcasts/{podcast['id']}", params={"limit": 3, "offset": 3}).json()
    assert [e["title"] for e in page["items"]] == ["Episode 04", "Episode 03", "Episode 02"] and page["total"] == 7


def an_entry(n: int) -> dict:
    """The feed's entry for the n-th episode, one a day from 2 Jan 2024."""
    when = datetime(2024, 1, 1, 10, tzinfo=timezone.utc) + timedelta(days=n)
    return {"guid": f"g{n}", "title": f"Episode {n:03d}", "path": "/ep1.mp3", "date": format_datetime(when, usegmt=True), "duration": "60"}


def test_only_the_latest_episodes_of_a_podcast_just_followed_are_new(web, site) -> None:
    site.episodes = [an_entry(n) for n in range(1, 9)]
    podcast = follow(web, site)
    assert (podcast["episodes"], podcast["unplayed"], podcast["new"]) == (8, 8, 3), "the back catalogue is not new"
    new = web.client.get("/api/podcasts/new").json()
    assert [e["title"] for e in new["items"]] == ["Episode 008", "Episode 007", "Episode 006"] and new["total"] == 3
    items = web.client.get(f"/api/podcasts/{podcast['id']}").json()["items"]
    assert [e["new"] for e in items] == [True, True, True] + [False] * 5
    assert web.client.get("/api/podcasts").json()["items"][0]["new"] == 3


def test_episodes_that_come_out_later_are_new_and_stop_being_new_when_heard(web, site) -> None:
    site.episodes = [an_entry(n) for n in range(1, 9)]
    podcast = follow(web, site)
    site.etag = '"v2"'
    site.episodes.append(an_entry(9))
    done = web.ctx.jobs.wait(web.client.post(f"/api/podcasts/{podcast['id']}/refresh").json()["job"]["id"], timeout=30)
    assert done.result["new"] == 1 and web.client.get("/api/podcasts").json()["items"][0]["new"] == 4
    nine = episode_ids(web, podcast["id"])["Episode 009"]
    assert web.client.get(f"/api/podcasts/episodes/{nine}").json()["new"] is True
    web.client.post(f"/api/podcasts/episodes/{nine}/played", json={"played": True})
    assert web.client.get(f"/api/podcasts/episodes/{nine}").json()["new"] is False
    assert [e["title"] for e in web.client.get("/api/podcasts/new").json()["items"]] == ["Episode 008", "Episode 007", "Episode 006"]


def test_a_half_heard_episode_stays_among_the_new_until_it_is_heard(web, site) -> None:
    site.episodes = [an_entry(n) for n in range(1, 9)]
    podcast = follow(web, site)
    old = episode_ids(web, podcast["id"])["Episode 002"]
    assert old not in [e["id"] for e in web.client.get("/api/podcasts/new").json()["items"]]
    web.client.post(f"/api/podcasts/episodes/{old}/progress", json={"position": 20})
    listed = web.client.get("/api/podcasts/new").json()
    assert old in [e["id"] for e in listed["items"]] and listed["total"] == 4
    assert web.client.get(f"/api/podcasts/episodes/{old}").json()["new"] is True
    web.client.post(f"/api/podcasts/episodes/{old}/progress", json={"position": 59})  # 60 s long: within 30 s of the end
    assert old not in [e["id"] for e in web.client.get("/api/podcasts/new").json()["items"]]
    assert web.client.get(f"/api/podcasts/episodes/{old}").json()["new"] is False


def test_an_episode_marked_unheard_is_new_again_and_marking_all_heard_clears_them(web, site) -> None:
    site.episodes = [an_entry(n) for n in range(1, 9)]
    podcast = follow(web, site)
    old = episode_ids(web, podcast["id"])["Episode 001"]
    web.client.post(f"/api/podcasts/episodes/{old}/played", json={"played": True})
    web.client.post(f"/api/podcasts/episodes/{old}/played", json={"played": False})
    assert web.client.get(f"/api/podcasts/episodes/{old}").json()["new"] is True and web.client.get("/api/podcasts/new").json()["total"] == 4
    assert web.client.post(f"/api/podcasts/{podcast['id']}/played").json() == {"marked": 8}
    assert web.client.get("/api/podcasts/new").json()["total"] == 0 and web.client.get("/api/podcasts").json()["items"][0]["new"] == 0


def test_a_check_that_finds_a_whole_history_flags_only_the_newest_of_it_as_new(web, site) -> None:
    site.episodes = [an_entry(n) for n in range(100, 103)]
    podcast = follow(web, site)  # three episodes, all new
    site.etag = '"v2"'
    site.episodes = [an_entry(n) for n in range(1, 103)]  # the feed now lists its older history too
    done = web.ctx.jobs.wait(web.client.post(f"/api/podcasts/{podcast['id']}/refresh").json()["job"]["id"], timeout=30)
    assert done.result["new"] == 99
    assert web.client.get(f"/api/podcasts/{podcast['id']}").json()["new"] == 3 + 20


def test_unknown_things_are_not_found(web) -> None:
    for path in ("/api/podcasts/999", "/api/podcasts/999/art", "/api/podcasts/episodes/999", "/api/podcasts/episodes/999/art", "/api/podcasts/episodes/999/stream"):
        assert web.client.get(path).status_code == 404, path
    assert web.client.post("/api/podcasts/999/refresh").status_code == 404


# ---------------------------------------------------------------- finding podcasts

def test_apples_directory_is_searched_and_says_which_shows_are_followed(web, site, monkeypatch) -> None:
    results = {"resultCount": 3, "results": [
        {"collectionId": 11, "collectionName": "The Test Show", "artistName": "Jo Host", "feedUrl": site.base + "/feed.xml", "artworkUrl600": "https://img.example/a.jpg", "primaryGenreName": "Comedy", "trackCount": 40},
        {"collectionId": 12, "collectionName": "Another", "artistName": "Pat", "feedUrl": "https://other.example/feed.xml", "artworkUrl100": "https://img.example/b.jpg"},
        {"collectionId": 13, "collectionName": "No feed address", "artistName": "Nobody"},
    ]}

    def answer(h):
        body = json.dumps(results).encode()
        h.send_response(200)
        h.send_header("Content-Type", "text/javascript; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    follow(web, site)
    with serve_script(answer) as itunes:
        monkeypatch.setenv("MTK_ITUNES_URL", itunes.url)
        found = web.client.get("/api/podcasts/search", params={"q": "test show", "country": "gb"}).json()["items"]
        assert web.client.get("/api/podcasts/search", params={"q": "   "}).json() == {"items": []}
    assert [(s["title"], s["followed_id"] is not None) for s in found] == [("The Test Show", True), ("Another", False)]
    assert found[0]["author"] == "Jo Host" and found[0]["genre"] == "Comedy" and found[0]["episodes"] == 40 and found[1]["art_url"] == "https://img.example/b.jpg"
    assert "term=test+show" in itunes.requests[0]["path"] and "country=GB" in itunes.requests[0]["path"] and "media=podcast" in itunes.requests[0]["path"]
    assert len(itunes.requests) == 1


def test_a_directory_that_is_down_is_a_bad_gateway_in_words(web, monkeypatch) -> None:
    with serve_script(lambda h: (h.send_response(500), h.send_header("Content-Length", "0"), h.end_headers())) as itunes:
        monkeypatch.setenv("MTK_ITUNES_URL", itunes.url)
        response = web.client.get("/api/podcasts/search", params={"q": "x"})
    assert response.status_code == 502 and "could not be reached" in response.json()["detail"]
    assert directory.search("   ") == []


# ---------------------------------------------------------------- pictures

def test_pictures_come_through_the_app_and_an_episode_without_one_uses_the_shows(web, site) -> None:
    podcast = follow(web, site)
    ids = episode_ids(web, podcast["id"])
    show = web.client.get(f"/api/podcasts/{podcast['id']}/art")
    episode = web.client.get(f"/api/podcasts/episodes/{ids['First']}/art")
    assert show.status_code == 200 and show.headers["content-type"] == "image/jpeg" and episode.content == show.content
    assert [r["path"] for r in site.server.requests].count("/art.png") == 1, "fetched once and kept"


# ---------------------------------------------------------------- listening

def test_an_episode_plays_whole_and_says_it_can_be_sought(web, site) -> None:
    podcast = follow(web, site)
    first = episode_ids(web, podcast["id"])["First"]  # /ep1.mp3: a host that honours Range
    response = web.client.get(f"/api/podcasts/episodes/{first}/stream")
    assert response.status_code == 200 and response.content == AUDIO and response.headers["content-type"] == "audio/mpeg"
    assert response.headers["content-length"] == str(len(AUDIO)) and response.headers["accept-ranges"] == "bytes"
    assert web.ctx.podcast_slots.used == 0, "the place is given back when the stream ends"


def test_a_part_of_an_episode_is_passed_on_when_the_host_honours_ranges(web, site) -> None:
    first = episode_ids(web, follow(web, site)["id"])["First"]
    part = web.client.get(f"/api/podcasts/episodes/{first}/stream", headers={"Range": "bytes=1000-1999"})
    assert part.status_code == 206 and part.content == AUDIO[1000:2000]
    assert part.headers["content-range"] == f"bytes 1000-1999/{len(AUDIO)}" and part.headers["content-length"] == "1000"
    assert part.headers["accept-ranges"] == "bytes", "so that the player knows it may seek"
    assert site.server.requests[-1]["headers"]["Range"] == "bytes=1000-1999"
    tail = web.client.get(f"/api/podcasts/episodes/{first}/stream", headers={"Range": "bytes=100000-"})
    assert tail.status_code == 206 and tail.content == AUDIO[100000:]


def test_a_host_that_ignores_ranges_is_made_to_seek_by_the_app(web, site) -> None:
    second = episode_ids(web, follow(web, site)["id"])["Second"]  # /ep2.mp3: always sends the whole file
    part = web.client.get(f"/api/podcasts/episodes/{second}/stream", headers={"Range": "bytes=50000-"})
    assert part.status_code == 206 and part.content == AUDIO[50000:]
    assert part.headers["content-range"] == f"bytes 50000-{len(AUDIO) - 1}/{len(AUDIO)}" and part.headers["content-length"] == str(len(AUDIO) - 50000)
    whole = web.client.get(f"/api/podcasts/episodes/{second}/stream")
    assert whole.status_code == 200 and whole.content == AUDIO and whole.headers["accept-ranges"] == "bytes"


def test_a_range_past_the_end_is_refused_like_the_host_does(web, site) -> None:
    first = episode_ids(web, follow(web, site)["id"])["First"]
    answer = web.client.get(f"/api/podcasts/episodes/{first}/stream", headers={"Range": "bytes=999999-"})
    assert answer.status_code == 416 and answer.headers["content-range"] == f"bytes */{len(AUDIO)}"
    assert web.ctx.podcast_slots.used == 0


def test_the_last_part_of_an_episode_can_be_asked_for_and_a_host_that_ignores_that_sends_it_all(web, site) -> None:
    ids = episode_ids(web, follow(web, site)["id"])
    tail = web.client.get(f"/api/podcasts/episodes/{ids['First']}/stream", headers={"Range": "bytes=-500"})
    assert tail.status_code == 206 and tail.content == AUDIO[-500:]
    assert tail.headers["content-range"] == f"bytes {len(AUDIO) - 500}-{len(AUDIO) - 1}/{len(AUDIO)}"
    assert site.server.requests[-1]["headers"]["Range"] == "bytes=-500"
    whole = web.client.get(f"/api/podcasts/episodes/{ids['Second']}/stream", headers={"Range": "bytes=-500"})  # /ep2.mp3 ignores Range
    assert whole.status_code == 200 and whole.content == AUDIO


@pytest.mark.parametrize("range_header", ["bytes=0-5,10-20", "items=0-5", "bytes=-", "bytes=abc-", "garbage"])
def test_ranges_the_app_does_not_understand_are_ignored_not_passed_on(web, site, range_header) -> None:
    first = episode_ids(web, follow(web, site)["id"])["First"]
    answer = web.client.get(f"/api/podcasts/episodes/{first}/stream", headers={"Range": range_header})
    assert answer.status_code == 200 and answer.content == AUDIO
    assert "Range" not in site.server.requests[-1]["headers"]


def test_an_address_that_is_not_audio_or_has_gone_is_a_bad_gateway_with_the_reason(web, site) -> None:
    site.episodes = [
        {"guid": "a", "title": "A page", "path": "/page.html", "date": "Tue, 03 Jun 2025 10:00:00 +0000", "duration": "60"},
        {"guid": "b", "title": "Gone", "path": "/missing.mp3", "date": "Tue, 02 Jun 2025 10:00:00 +0000", "duration": "60"},
    ]
    ids = episode_ids(web, follow(web, site)["id"])
    page = web.client.get(f"/api/podcasts/episodes/{ids['A page']}/stream")
    gone = web.client.get(f"/api/podcasts/episodes/{ids['Gone']}/stream")
    assert page.status_code == 502 and "not audio" in page.json()["detail"]
    assert gone.status_code == 502 and "not at that address any more" in gone.json()["detail"] or "error 404" in gone.json()["detail"]
    assert web.ctx.podcast_slots.used == 0


def test_an_episode_whose_address_now_points_inside_is_never_connected_to(web, site) -> None:
    first = episode_ids(web, follow(web, site)["id"])["First"]
    before = len(site.server.requests)
    guard.allow_local(False)
    try:
        answer = web.client.get(f"/api/podcasts/episodes/{first}/stream")
    finally:
        guard.allow_local(True)
    assert answer.status_code == 502 and "not on the internet" in answer.json()["detail"] and len(site.server.requests) == before


def test_only_so_many_episodes_can_be_passed_on_at_once(web, site) -> None:
    from musictoolkit.net.proxy import Slots

    first = episode_ids(web, follow(web, site)["id"])["First"]
    web.ctx.podcast_slots = Slots(0)
    assert web.client.get(f"/api/podcasts/episodes/{first}/stream").status_code == 429
