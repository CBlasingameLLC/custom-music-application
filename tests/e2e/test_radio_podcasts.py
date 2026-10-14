"""Radio and podcasts, driven like a person would: real clicks in a real browser, real audio elements, against fake stations, a fake
Radio Browser, a fake Apple directory and a fake podcast host on this computer (nothing here touches the internet)."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect

from musictoolkit.net import guard
from musictoolkit.radio import directory as radio_directory
from tests.e2e.conftest import add_library, api, play, wait_until
from tests.fake_streams import PodcastSite, json_script, live_script, mp3_frames, serve_script

expect.set_options(timeout=10_000)

TONE = (Path(__file__).parent.parent / "fixtures" / "tone_12s.mp3").read_bytes()  # 12 s of sound that can really be played
PLAYER = "import('/static/js/player.js')"
LONG_NOTES = "The long notes of episode six, which go on and on. " * 8  # over the 240 characters a row shows


def bar(page):
    return page.locator(".playerbar")


def state(page) -> dict:
    return page.evaluate(
        f"{PLAYER}.then((m) => {{ const s = m.player.get(); const it = m.current(s); "
        "return { playing: s.playing, position: s.position, kind: it ? (it.kind || 'track') : null, speed: s.speed, liveTitle: s.liveTitle, error: s.error, "
        "rates: m.deckState().decks.map((d) => d.rate), loaded: m.deckState().decks.filter((d) => d.track !== null).length }; })"
    )


def loaded_addresses(page) -> list[str]:
    """The addresses the player's two audio elements have been told to load (the empty one is left out)."""
    return page.evaluate(f"{PLAYER}.then((m) => m.deckState().decks.map((d) => d.src).filter(Boolean))")


def playing(page) -> bool:
    return page.evaluate(f"{PLAYER}.then((m) => m.player.get().playing)")


@pytest.fixture(autouse=True)
def local_hosts():
    """The fake hosts are on this computer, which the app refuses to connect to as it really runs. (The directory remembers its answers for a few
    minutes: a test starts with none, so that it asks the fake one it was given.)"""
    radio_directory.clear_cache()
    with guard.local_urls_allowed():
        yield


@pytest.fixture
def station():
    """A live radio station: it never ends, and names the title that is on."""
    with serve_script(live_script(mp3_frames(TONE), change_every=3600)) as server:
        yield server


@pytest.fixture
def switch_station():
    """A station that works until `.broken` is set: then it answers "not found" to everything."""
    inner = live_script(mp3_frames(TONE), change_every=3600)
    control = SimpleNamespace(broken=False)

    def script(h):
        if control.broken:
            h.send_response(404)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        inner(h)

    with serve_script(script) as server:
        control.url = server.url
        control.requests = server.requests
        yield control


@pytest.fixture
def directory(monkeypatch, station):
    """A Radio Browser on this computer, listing two stations (the first is `station`)."""
    stations = [
        {"stationuuid": "aaaaaaaa-1111-2222-3333-444444444444", "name": "Directory FM", "url_resolved": station.url + "/live", "country": "Norway", "countrycode": "NO",
         "codec": "MP3", "bitrate": 128, "tags": "jazz,smooth", "votes": 10},
        {"stationuuid": "bbbbbbbb-1111-2222-3333-444444444444", "name": "Second Directory Station", "url_resolved": station.url + "/live2", "country": "Chile", "countrycode": "CL",
         "codec": "AAC", "bitrate": 64, "tags": "rock", "votes": 3},
    ]
    routes = {
        "/stations/search": stations,
        "/tags": [{"name": "jazz", "stationcount": 100}, {"name": "rock", "stationcount": 90}],
        "/countries": [{"name": "Norway", "iso_3166_1": "NO", "stationcount": 5}, {"name": "Chile", "iso_3166_1": "CL", "stationcount": 3}],
    }
    with serve_script(json_script(routes)) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url)
        yield server


@pytest.fixture
def big_directory(monkeypatch, station):
    """A Radio Browser that knows 45 stations and answers what it is asked (name, tag, country, order, offset, limit): odd numbers play rock and
    even ones jazz, the first 20 are in Norway and the rest in Chile, and the higher the number the better the sound."""

    def entry(n: int) -> dict:
        return {
            "stationuuid": f"{n:08d}-1111-2222-3333-444444444444", "name": f"Station {n:02d}", "url_resolved": f"{station.url}/live{n}", "country": "Norway" if n <= 20 else "Chile",
            "countrycode": "NO" if n <= 20 else "CL", "codec": "MP3", "bitrate": 64 + n, "tags": "rock" if n % 2 else "jazz", "clickcount": 1000 - n, "votes": n,
        }

    def search(asked: dict) -> list[dict]:
        found = [entry(n) for n in range(1, 46)]
        if asked.get("name"):
            found = [e for e in found if asked["name"].lower() in e["name"].lower()]
        if asked.get("tag"):
            found = [e for e in found if asked["tag"] in e["tags"].split(",")]
        if asked.get("countrycode"):
            found = [e for e in found if e["countrycode"] == asked["countrycode"]]
        found.sort(key=lambda e: e[asked["order"]], reverse=asked["reverse"] == "true")
        return found[int(asked.get("offset", 0)) :][: int(asked["limit"])]

    routes = {
        "/stations/search": search,
        "/tags": [{"name": "jazz", "stationcount": 22}, {"name": "rock", "stationcount": 23}],
        "/countries": [{"name": "Norway", "iso_3166_1": "NO", "stationcount": 20}, {"name": "Chile", "iso_3166_1": "CL", "stationcount": 25}],
    }
    with serve_script(json_script(routes)) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url)
        yield server


@pytest.fixture
def show():
    """A podcast host with six 12 s episodes that can really be played."""
    site = PodcastSite(TONE)
    site.episodes = [
        {
            "guid": f"g{n}", "title": f"Episode {n}", "path": "/ep1.mp3", "date": f"Tue, {n:02d} Jun 2025 10:00:00 +0000", "duration": "0:00:12",
            "description": LONG_NOTES if n == 6 else f"Notes for episode {n}.",
        }
        for n in range(1, 7)
    ]
    with serve_script(site.script()) as server:
        site.base = server.url
        site.server = server
        yield site


@pytest.fixture
def apple(monkeypatch, show):
    """Apple's podcast directory on this computer, knowing one show (the fake host's)."""
    routes = {"/search": {"resultCount": 1, "results": [{"collectionId": 7, "collectionName": "The Test Show", "artistName": "Jo Host", "feedUrl": show.base + "/feed.xml",
                                                        "primaryGenreName": "Comedy", "trackCount": 6}]}}
    with serve_script(json_script(routes)) as server:
        monkeypatch.setenv("MTK_ITUNES_URL", server.url)
        yield server


def episode(page, podcast_id: int, title: str) -> dict:
    """The app's own record of an episode, found by title (its number is the order the feed was read in, not the title's)."""
    return next(e for e in api(page, f"/podcasts/{podcast_id}")["items"] if e["title"] == title)


def add_station(page, station) -> None:
    page.evaluate("location.hash = '#/radio'")
    page.get_by_role("tab", name="Add by address").click()
    page.get_by_label("Stream address").fill(station.url + "/live")
    page.get_by_role("button", name="Add station").click()
    expect(page.locator(".station-card")).to_have_count(1)


def follow_show(page, show) -> int:
    page.evaluate("location.hash = '#/podcasts'")
    page.get_by_role("tab", name="Find").click()
    page.get_by_label("Feed address").fill(show.base + "/feed.xml")
    page.get_by_role("button", name="Follow this feed").click()
    expect(page.get_by_text('Following "The Test Show"')).to_be_visible()
    return api(page, "/podcasts")["items"][0]["id"]


# ---------------------------------------------------------------- radio

def test_a_station_typed_in_plays_live_with_the_title_it_carries(page, station):
    add_station(page, station)
    expect(page.locator(".station-card .card-title")).to_have_text("Live FM")  # the name the stream gave itself
    page.get_by_role("button", name="Play Live FM").click()

    expect(bar(page).locator(".live-badge")).to_be_visible()
    expect(page.locator(".station-card.current .live-flag")).to_have_count(1)  # the station on the screen shows that it is the one playing
    expect(bar(page).locator(".pb-text .t1")).to_have_text("Miles Davis - So What", timeout=15_000)  # the stream's own title, asked for by the player
    expect(bar(page).locator(".pb-text .t2")).to_have_text("Live FM")
    expect(page.get_by_label("Seek")).to_have_count(0)  # live: nothing to seek in
    assert page.title() == "▶ Miles Davis - So What · Live FM"
    assert state(page)["kind"] == "station" and state(page)["playing"] is True
    # What the operating system's media overlay (and the keyboard's media keys) shows is the same title and station.
    overlay = page.evaluate("({ title: navigator.mediaSession.metadata.title, artist: navigator.mediaSession.metadata.artist })")
    assert overlay == {"title": "Miles Davis - So What", "artist": "Live FM"}

    wait_until(page, f"{PLAYER}.then((m) => m.player.get().position > 7)", 20)
    first = loaded_addresses(page)
    bar(page).get_by_role("button", name="Pause", exact=True).click()
    wait_until(page, f"{PLAYER}.then((m) => !m.player.get().playing)")
    expect(page.locator(".station-card.current")).to_have_count(1)  # still the one in the player, but no longer sounding
    expect(page.locator(".station-card .live-flag")).to_have_count(0)
    connections = len(station.requests)
    bar(page).get_by_role("button", name="Play", exact=True).click()  # a live stream is joined afresh
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    assert state(page)["position"] < 5, "it started again from the live edge, not from where it was paused (old sound)"
    assert len(station.requests) == connections + 1, "and by a new connection to the station"
    second = loaded_addresses(page)
    assert len(first) == len(second) == 1 and first != second, "each join of a live stream asks for an address of its own (the browser would play the first one's sound again)"


def test_stations_from_the_directory_are_listed_kept_and_played(page, station, directory):
    page.evaluate("location.hash = '#/radio'")
    page.get_by_role("tab", name="Browse").click()
    expect(page.locator(".station-card .card-title")).to_have_text(["Directory FM", "Second Directory Station"])
    expect(page.get_by_role("button", name="jazz", exact=True)).to_be_visible()  # the kinds of music the directory knows
    expect(page.get_by_label("Search stations by name")).to_be_visible()

    page.get_by_role("button", name="Favorite: Directory FM").click()
    expect(page.get_by_role("button", name="Favorite: Directory FM")).to_have_attribute("aria-pressed", "true")
    assert api(page, "/radio/stations?scope=favorites")["counts"]["favorites"] == 1
    page.get_by_role("tab", name=re.compile("Favorites")).click()
    expect(page.locator(".station-card .card-title")).to_have_text("Directory FM")

    page.get_by_role("tab", name="Browse").click()
    page.get_by_role("button", name="Play Second Directory Station").click()  # kept first, so that there is somewhere to play it from
    expect(bar(page).locator(".live-badge")).to_be_visible()
    wait_until(page, "fetch('/api/radio/stations?scope=recent').then((r) => r.json()).then((s) => s.counts.recent === 1)")
    page.get_by_role("tab", name=re.compile("Recent")).click()
    expect(page.locator(".station-card .card-title")).to_have_text("Second Directory Station")
    assert api(page, "/radio/stations?scope=favorites")["counts"]["favorites"] == 1, "playing a station does not make it a favorite"

    # The heart in the player bar keeps what is playing among the favorites (and the list on the screen follows).
    bar(page).get_by_role("button", name="Add to favorites").click()
    expect(bar(page).get_by_role("button", name="Remove from favorites")).to_have_attribute("aria-pressed", "true")
    page.get_by_role("tab", name=re.compile("Favorites")).click()
    expect(page.locator(".station-card .card-title")).to_have_text(["Directory FM", "Second Directory Station"], timeout=10_000)
    assert api(page, "/radio/stations?scope=favorites")["counts"]["favorites"] == 2


def test_a_directory_that_cannot_be_reached_says_so_and_can_be_asked_again(page, monkeypatch, directory):
    monkeypatch.setenv("MTK_RADIO_BROWSER_URL", "http://127.0.0.1:9")  # nothing listens there (until the directory comes back, below)
    page.allowed_statuses |= {502}
    page.evaluate("location.hash = '#/radio'")
    page.get_by_role("tab", name="Browse").click()
    expect(page.get_by_role("heading", name="The directory could not be reached")).to_be_visible()
    expect(page.locator(".station-card")).to_have_count(0)

    monkeypatch.setenv("MTK_RADIO_BROWSER_URL", directory.url)
    page.get_by_role("button", name="Try again").click()
    expect(page.locator(".station-card .card-title")).to_have_text(["Directory FM", "Second Directory Station"])
    expect(page.get_by_role("heading", name="The directory could not be reached")).to_have_count(0)
    expect(page.get_by_role("button", name="jazz", exact=True)).to_be_visible()  # the kinds of music were asked for again too


def test_the_directory_can_be_searched_by_name_kind_and_country_sorted_and_paged(page, big_directory):
    page.evaluate("location.hash = '#/radio'")
    page.get_by_role("tab", name="Browse").click()
    titles = page.locator(".station-card .card-title")
    expect(titles).to_have_count(40)  # the first page...
    expect(titles.first).to_have_text("Station 01")  # ...of the most listened to
    page.get_by_role("button", name="Show more stations").click()
    expect(titles).to_have_count(45)
    expect(page.get_by_role("button", name="Show more stations")).to_have_count(0)

    search = page.get_by_label("Search stations by name")
    search.fill("station 4")
    expect(titles).to_have_text([f"Station {n}" for n in range(40, 46)])
    search.fill("")
    expect(titles).to_have_count(40)

    rock = page.get_by_role("button", name="rock", exact=True)
    rock.click()
    expect(rock).to_have_attribute("aria-pressed", "true")
    expect(titles).to_have_count(23)  # the odd numbers
    page.get_by_label("Country").select_option(label="Chile")
    expect(titles).to_have_count(13)  # the odd numbers from 21 on
    rock.click()  # pressed again: no longer chosen
    expect(rock).to_have_attribute("aria-pressed", "false")
    expect(titles).to_have_count(25)  # all of Chile

    page.get_by_label("Sort by").select_option(label="Best sound")
    expect(titles.first).to_have_text("Station 45")
    page.get_by_label("Sort by").select_option(label="Name")
    expect(titles.first).to_have_text("Station 21")
    page.get_by_label("Country").select_option(label="Anywhere")
    expect(titles.first).to_have_text("Station 01")
    asked = [r["path"] for r in big_directory.requests if r["path"].startswith("/stations/search")]
    assert any("order=bitrate" in a for a in asked) and any("order=name&reverse=false" in a for a in asked) and any("countrycode=CL" in a for a in asked)


def test_a_station_can_be_renamed_and_removed(page, station):
    add_station(page, station)
    page.get_by_role("button", name="More about Live FM").click()
    page.get_by_role("menuitem", name="Rename…").click()
    dialog = page.get_by_role("dialog", name="Rename station")
    dialog.get_by_label("Name").fill("My Jazz")
    dialog.get_by_role("button", name="Save").click()
    expect(page.locator(".station-card .card-title")).to_have_text("My Jazz")
    assert api(page, "/radio/stations")["items"][0]["name"] == "My Jazz"

    page.get_by_role("button", name="More about My Jazz").click()
    page.get_by_role("menuitem", name="Remove from my stations").click()
    page.get_by_role("dialog", name="Remove station").get_by_role("button", name="Remove").click()
    expect(page.get_by_role("heading", name="No favorite stations yet")).to_be_visible()
    assert api(page, "/radio/stations")["items"] == []


def test_an_address_that_is_not_a_station_is_not_kept_and_says_why(page, switch_station):
    switch_station.broken = True  # it answers "not found"
    page.allowed_statuses |= {422}
    page.evaluate("location.hash = '#/radio'")
    page.get_by_role("tab", name="Add by address").click()
    page.get_by_label("Stream address").fill(switch_station.url + "/live")
    page.get_by_role("button", name="Add station").click()
    expect(page.get_by_role("alert").filter(has_text="The station is not at that address any more.")).to_be_visible()
    assert api(page, "/radio/stations")["items"] == []


def test_a_station_that_goes_missing_says_why_it_cannot_be_played(page, switch_station):
    add_station(page, switch_station)
    switch_station.broken = True
    page.allowed_statuses |= {502}
    page.get_by_role("button", name="Play Live FM").click()
    expect(page.locator(".toast.error")).to_contain_text("Can't play \"Live FM\": The station is not at that address any more.", timeout=15_000)
    assert state(page)["playing"] is False


def test_a_station_that_drops_the_connection_is_joined_again(page):
    # Eight blocks are exactly 32 KiB, which the browser keeps whole: asked for the same address a second time it would play that sound over
    # again from its own memory, instead of making a new connection to the station (each join of a live stream has an address of its own).
    with serve_script(live_script(mp3_frames(TONE), blocks=8)) as flaky:
        add_station(page, flaky)
        page.get_by_role("button", name="Play Live FM").click()
        wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
        # It hangs up after a moment (the player plays what it has already received first); then it joins the station again by itself (the requests show the new connection).
        connections = lambda: len([r for r in flaky.requests if r["path"] == "/live"])  # noqa: E731
        deadline = time.time() + 20
        while connections() < 3 and time.time() < deadline:
            time.sleep(0.2)
        assert connections() >= 3, "the check when it was added, the first listen, and a new connection after the hang-up"
        wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
        assert state(page)["error"] is None


def test_a_station_that_keeps_dropping_is_given_up_on_and_says_so(page):
    with serve_script(live_script(mp3_frames(TONE), blocks=4)) as flaky:
        add_station(page, flaky)
        page.get_by_role("button", name="Play Live FM").click()
        expect(page.locator(".toast.error")).to_contain_text('"Live FM" stopped playing, and could not be joined again', timeout=90_000)
        assert state(page)["playing"] is False
        connections = lambda: len([r for r in flaky.requests if r["path"] == "/live"])  # noqa: E731
        tried = connections()
        assert tried >= 5, "the check when it was added, the first listen, and three more connections"
        time.sleep(1)  # and it does not carry on trying
        assert connections() == tried


# When a live stream hangs up, the player waits a moment (1.5 s the first time) before joining it again. These run something the instant that wait
# begins, inside the page (so that no timer can get in between) and report what the player says about it.
IN_THE_WAIT = """(async () => {{
  const m = await {player};
  const deadline = Date.now() + 40000;
  while (!m.deckState().rejoining) {{
    if (Date.now() > deadline) return 'the stream never hung up';
    await new Promise((done) => setTimeout(done, 15));
  }}
  {act}
  return m.deckState().rejoining ? 'still waiting to rejoin' : 'ok';
}})()"""


def test_pausing_while_a_dropped_station_waits_to_be_rejoined_keeps_it_stopped(page):
    with serve_script(live_script(mp3_frames(TONE), blocks=4)) as flaky:
        add_station(page, flaky)
        page.get_by_role("button", name="Play Live FM").click()
        # Stop (a media key, the sleep timer) in that moment must be obeyed: no station comes back on by itself afterwards.
        assert page.evaluate(IN_THE_WAIT.format(player=PLAYER, act="m.pause();")) == "ok"
        connections = lambda: len([r for r in flaky.requests if r["path"] == "/live"])  # noqa: E731
        tried = connections()
        time.sleep(4)  # longer than the wait it was in
        assert connections() == tried and state(page)["playing"] is False
        bar(page).get_by_role("button", name="Play", exact=True).click()  # and Play still brings it back
        wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")


def test_pressing_play_while_a_dropped_station_waits_joins_it_at_once_and_only_once(page):
    with serve_script(live_script(mp3_frames(TONE), blocks=4)) as flaky:
        add_station(page, flaky)
        page.get_by_role("button", name="Play Live FM").click()
        assert page.evaluate(IN_THE_WAIT.format(player=PLAYER, act="m.toggle();")) == "ok"  # what the Play button does
        pressed = time.monotonic()
        wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)", 10)
        time.sleep(2.5)  # past the end of the wait that was cancelled (1.5 s): it must not join a second time
        joined = [r for r in flaky.requests if r["path"] == "/live" and r["at"] > pressed - 1.0]
        assert len(joined) == 1, f"joined {len(joined)} times after Play was pressed"


def test_listening_to_radio_and_podcasts_never_enters_the_music_history(page, live, station, show):
    add_library(page, live)  # there are songs (number 1 among them): a station or episode taken for one would be logged against it
    add_station(page, station)
    page.get_by_role("button", name="Play Live FM").click()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().position > 11)", 20)  # longer than a song needs to be listened to before it is logged

    podcast_id = follow_show(page, show)
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    page.get_by_role("button", name="Play Episode 6").click()
    heard = episode(page, podcast_id, "Episode 6")["id"]
    wait_until(page, f"fetch('/api/podcasts/episodes/{heard}').then((r) => r.json()).then((e) => e.played)", 30)  # heard to the end: all of its 12 s

    assert api(page, "/history/top-artists?limit=5")["total_plays"] == 0
    assert api(page, "/history/recent?limit=5")["items"] == []


# ---------------------------------------------------------------- podcasts

def test_a_podcast_is_found_followed_and_its_latest_episodes_are_new(page, show, apple):
    page.evaluate("location.hash = '#/podcasts'")
    expect(page.get_by_role("heading", name="You don't follow any podcasts yet")).to_be_visible()
    page.get_by_role("button", name="Find podcasts").click()
    page.get_by_label("Search for a podcast").fill("test show")
    expect(page.locator(".result-card .card-title")).to_have_text("The Test Show")
    page.get_by_role("button", name="Follow The Test Show").click()
    expect(page.locator(".result-card").get_by_role("link", name="Following")).to_be_visible()

    page.get_by_role("tab", name=re.compile("Subscriptions")).click()
    card = page.locator(".podcast-card")
    expect(card).to_have_count(1)
    expect(card.locator(".new-flag")).to_have_text("3 new")  # the latest three of a show just followed: its back catalogue is not new
    expect(page.get_by_role("tab", name=re.compile("New episodes"))).to_contain_text("3")
    card.click()
    expect(page.get_by_role("heading", name="The Test Show", level=1)).to_be_visible()
    expect(page.locator(".episode")).to_have_count(6)
    expect(page.locator(".episode").first).to_contain_text("Episode 6")
    expect(page.locator(".episode .badge")).to_have_count(3)

    page.get_by_role("link", name="Podcasts", exact=True).click()
    page.get_by_role("tab", name=re.compile("New episodes")).click()
    expect(page.locator(".episode .ep-title")).to_have_text([re.compile("Episode 6"), re.compile("Episode 5"), re.compile("Episode 4")])


def test_a_podcast_that_is_not_a_feed_is_not_followed_and_says_why(page, show):
    page.allowed_statuses |= {422}
    page.evaluate("location.hash = '#/podcasts'")
    page.get_by_role("tab", name="Find").click()
    page.get_by_label("Feed address").fill(show.base + "/page.html")
    page.get_by_role("button", name="Follow this feed").click()
    expect(page.get_by_role("alert").filter(has_text="no RSS channel")).to_be_visible()
    assert api(page, "/podcasts")["items"] == []


def test_an_episode_resumes_where_listening_stopped_and_is_heard_at_the_end(page, show):
    podcast_id = follow_show(page, show)
    six = episode(page, podcast_id, "Episode 6")["id"]
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    page.get_by_role("button", name="Play Episode 6").click()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    # Near the end of the 12 s, but not heard: stop there.
    page.evaluate(f"{PLAYER}.then((m) => {{ m.seek(10.4); m.pause(); }})")
    wait_until(page, f"fetch('/api/podcasts/episodes/{six}').then((r) => r.json()).then((e) => e.position > 10)")
    stopped = api(page, f"/podcasts/episodes/{six}")
    assert stopped["played"] is False and 10.3 < stopped["position"] < 10.6
    expect(page.locator(".episode").first.locator(".progress")).to_be_visible()  # the row shows how far along it is

    page.get_by_role("button", name="Play Episode 5").click()  # something else in between
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    page.get_by_role("button", name="Play Episode 6").click()  # and back: it picks up a few seconds before where it stopped
    wait_until(page, f"{PLAYER}.then((m) => m.current().title === 'Episode 6' && m.player.get().playing && m.player.get().position > 0)", 15)
    assert 7 < state(page)["position"] < 9.9, "a few seconds before where it stopped (10.4), not from the start and not exactly where it stopped"

    page.evaluate(f"{PLAYER}.then((m) => m.seek(11.6))")  # the last moments
    wait_until(page, f"fetch('/api/podcasts/episodes/{six}').then((r) => r.json()).then((e) => e.played)", 15)
    heard = api(page, f"/podcasts/episodes/{six}")
    assert heard["position"] == 0 and heard["new"] is False


def test_the_place_in_an_episode_is_kept_when_moving_on_and_when_the_page_closes(page, show):
    podcast_id = follow_show(page, show)
    six, five = episode(page, podcast_id, "Episode 6")["id"], episode(page, podcast_id, "Episode 5")["id"]
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    page.get_by_role("button", name="Play Episode 6").click()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    expect(bar(page).locator(".pb-text .t1")).to_have_text("Episode 6")
    expect(bar(page).locator(".pb-text .t2")).to_have_text("The Test Show")
    page.get_by_role("button", name="Pause Episode 6").click()  # the row's own button pauses what it is playing...
    wait_until(page, f"{PLAYER}.then((m) => !m.player.get().playing)")
    resumed = state(page)["position"]
    page.get_by_role("button", name="Play Episode 6").click()  # ...and carries on from the same place
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing && m.player.get().position > {resumed})")
    assert state(page)["position"] < resumed + 3, "it carried on; it did not start again"

    page.evaluate(f"{PLAYER}.then((m) => m.seek(10.4))")  # near the end of the 12 s, but not heard
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing && m.player.get().position > 10.3)")
    bar(page).get_by_role("button", name="Next", exact=True).click()  # on to Episode 5 without pausing
    wait_until(page, f"fetch('/api/podcasts/episodes/{six}').then((r) => r.json()).then((e) => e.position > 10)")
    assert episode(page, podcast_id, "Episode 6")["played"] is False

    # Episode 5 is playing now: closing the page (the window, in the app) keeps its place with a request that outlives the page.
    page.evaluate(f"{PLAYER}.then((m) => m.seek(10.4))")
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing && m.player.get().position > 10.3)")
    page.reload()
    page.wait_for_selector(".sidebar")
    wait_until(page, f"fetch('/api/podcasts/episodes/{five}').then((r) => r.json()).then((e) => e.position > 10)")


def test_an_episode_that_plays_out_is_heard_even_when_nothing_follows_it(page, show):
    podcast_id = follow_show(page, show)
    six = episode(page, podcast_id, "Episode 6")["id"]
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    page.get_by_role("button", name="More about Episode 6").click()
    page.get_by_role("menuitem", name="Add to queue").click()  # the only thing in the queue: nothing comes after it to report where it stopped
    bar(page).get_by_role("button", name="Play", exact=True).click()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing && m.player.get().position > 1)")  # the first report of where it has got to has been sent
    page.evaluate(f"{PLAYER}.then((m) => m.seek(11.2))")  # the last moments; the next periodic report is ten seconds away
    wait_until(page, f"fetch('/api/podcasts/episodes/{six}').then((r) => r.json()).then((e) => e.played)", 15)
    wait_until(page, f"{PLAYER}.then((m) => !m.player.get().playing)")
    assert episode(page, podcast_id, "Episode 6")["position"] == 0


def test_episodes_have_skip_buttons_and_a_speed_that_songs_and_stations_do_not_use(page, live, show):
    add_library(page, live)
    podcast_id = follow_show(page, show)
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    page.get_by_role("button", name="Play Episode 6").click()
    expect(bar(page).get_by_role("button", name="Back 15 seconds")).to_be_visible()
    expect(bar(page).get_by_role("button", name="Forward 30 seconds")).to_be_visible()

    bar(page).get_by_role("button", name=re.compile("^Playback speed 1×")).click()
    expect(bar(page).get_by_role("button", name=re.compile("^Playback speed 1.25×"))).to_be_visible()
    assert state(page)["rates"] == [1.25, 1.25] and state(page)["speed"] == 1.25
    page.evaluate(f"{PLAYER}.then((m) => m.seek(8))")
    bar(page).get_by_role("button", name="Back 15 seconds").click()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().position < 3)")  # back past the start: the start
    six = episode(page, podcast_id, "Episode 6")["id"]
    assert episode(page, podcast_id, "Episode 6")["played"] is False
    bar(page).get_by_role("button", name="Forward 30 seconds").click()  # past the end of 12 s: to the end, where it counts as heard
    wait_until(page, f"fetch('/api/podcasts/episodes/{six}').then((r) => r.json()).then((e) => e.played)", 5)  # at once: playing on would take 12 s

    page.evaluate("location.hash = '#/songs'")
    page.wait_for_selector(".trow:not(.skeleton)")
    play(page, "Glacier")
    expect(bar(page).get_by_role("button", name="Shuffle: off")).to_be_visible()
    expect(bar(page).get_by_role("button", name=re.compile("^Playback speed"))).to_have_count(0)
    assert state(page)["rates"] == [1, 1], "songs always play at their own speed"


def test_a_podcast_can_be_unfollowed(page, show):
    podcast_id = follow_show(page, show)
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    page.get_by_role("button", name="Unfollow").click()
    page.get_by_role("dialog").get_by_role("button", name="Unfollow").click()
    expect(page.get_by_role("heading", name="You don't follow any podcasts yet")).to_be_visible()
    assert api(page, "/podcasts")["items"] == []


def test_a_long_description_is_cut_short_until_it_is_asked_for(page, show):
    podcast_id = follow_show(page, show)
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    row = page.locator(".episode").filter(has_text="Episode 6")
    more = row.get_by_role("button", name="Show more")
    expect(more).to_have_attribute("aria-expanded", "false")
    expect(row.locator(".ep-desc")).not_to_have_class(re.compile(r"\bopen\b"))
    more.click()
    expect(row.get_by_role("button", name="Show less")).to_have_attribute("aria-expanded", "true")
    expect(row.locator(".ep-desc")).to_have_class(re.compile(r"\bopen\b"))
    short = page.locator(".episode").filter(has_text="Episode 5")
    expect(short.locator(".ep-desc")).to_have_text("Notes for episode 5.")
    expect(short.get_by_role("button", name="Show more")).to_have_count(0)  # short notes need no button


def test_now_playing_tells_what_a_station_and_an_episode_are(page, station, show):
    add_station(page, station)
    page.get_by_role("button", name="Play Live FM").click()
    page.evaluate("location.hash = '#/now'")
    expect(page.get_by_role("heading", name="Miles Davis - So What", level=1)).to_be_visible(timeout=15_000)
    expect(page.locator(".np-meta .live-badge")).to_be_visible()
    expect(page.locator(".np-meta").get_by_role("link", name="Live FM")).to_have_attribute("href", "#/radio")
    expect(page.get_by_role("tab")).to_have_text(["Up next", "Sound"])  # no lyrics for a station

    podcast_id = follow_show(page, show)
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    page.get_by_role("button", name="Play Episode 3").click()
    page.evaluate("location.hash = '#/now'")
    expect(page.get_by_role("heading", name="Episode 3", level=1)).to_be_visible()
    expect(page.get_by_role("tab")).to_have_text(["About", "Up next", "Sound"])
    notes = page.get_by_role("region", name="About this episode")
    expect(notes).to_contain_text("Notes for episode 3.")
    expect(notes.get_by_role("link", name="More from The Test Show")).to_have_attribute("href", f"#/podcast/{podcast_id}")


def test_episodes_can_be_marked_heard_and_queued_from_their_menu_and_a_whole_show_at_once(page, show):
    podcast_id = follow_show(page, show)
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    row = page.locator(".episode").filter(has_text="Episode 2")

    def menu(row, title: str) -> None:
        row.scroll_into_view_if_needed()
        page.evaluate("new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")  # a scroll still on its way would close the menu
        row.get_by_role("button", name=f"More about {title}").click()
        expect(page.locator(".menu")).to_be_visible()

    menu(row, "Episode 2")
    page.get_by_role("menuitem", name="Mark as heard").click()
    expect(row.locator(".ep-meta")).to_contain_text("Heard")
    assert episode(page, podcast_id, "Episode 2")["played"] is True
    page.get_by_role("button", name="Not heard", exact=True).click()  # only what has not been heard
    expect(page.locator(".episode")).to_have_count(5)
    page.get_by_role("button", name="All", exact=True).click()
    expect(page.locator(".episode")).to_have_count(6)
    menu(row, "Episode 2")
    page.get_by_role("menuitem", name="Mark as not heard").click()
    expect(row.locator(".ep-meta")).not_to_contain_text("Heard")
    assert episode(page, podcast_id, "Episode 2")["played"] is False

    # The queue: one at the end, another at the end, and a third to be next.
    menu(row, "Episode 2")
    page.get_by_role("menuitem", name="Add to queue").click()
    expect(page.get_by_text('Added "Episode 2" to the queue')).to_be_visible()
    menu(page.locator(".episode").filter(has_text="Episode 4"), "Episode 4")
    page.get_by_role("menuitem", name="Add to queue").click()
    expect(page.get_by_text('Added "Episode 4" to the queue')).to_be_visible()
    menu(page.locator(".episode").filter(has_text="Episode 3"), "Episode 3")
    page.get_by_role("menuitem", name="Play next").click()
    expect(page.get_by_text('"Episode 3" plays next')).to_be_visible()
    queued = page.evaluate(f"{PLAYER}.then((m) => {{ const s = m.player.get(); return s.order.map((i) => s.items[i].title); }})")
    assert queued == ["Episode 2", "Episode 3", "Episode 4"]

    menu(page.locator(".episode").filter(has_text="Episode 6"), "Episode 6")
    page.get_by_role("menuitem", name="Mark as heard").click()
    expect(page.locator(".episode").filter(has_text="Episode 6").locator(".ep-meta")).to_contain_text("Heard")
    page.locator(".hero-actions").get_by_role("button", name="Play", exact=True).click()  # starts with the newest episode not heard yet
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing && m.current().title === 'Episode 5')")

    page.get_by_role("button", name="Mark all as heard").click()
    page.get_by_role("dialog", name="Mark all as heard").get_by_role("button", name="Mark as heard").click()
    expect(page.get_by_text("Marked 5 episodes as heard")).to_be_visible()
    expect(page.locator(".episode.heard")).to_have_count(6)
    expect(page.get_by_role("button", name="Mark all as heard")).to_be_disabled()  # nothing left to mark
    page.get_by_role("button", name="Not heard", exact=True).click()
    expect(page.get_by_text("You have heard every episode.")).to_be_visible()
    assert api(page, "/podcasts/new")["total"] == 0, "what has been heard is not new any more"


def test_a_check_finds_a_new_episode_and_a_feed_that_fails_is_reported(page, show):
    podcast_id = follow_show(page, show)
    show.etag = '"v2"'
    show.episodes.insert(0, {"guid": "g7", "title": "Episode 7", "path": "/ep1.mp3", "date": "Tue, 07 Jul 2025 10:00:00 +0000", "duration": "0:00:12"})
    page.evaluate("location.hash = '#/podcasts'")
    page.get_by_role("tab", name=re.compile("Subscriptions")).click()
    page.get_by_role("button", name="Check for new episodes").click()
    expect(page.locator(".podcast-card .new-flag")).to_have_text("4 new")  # the check finished and the screen read the shows again
    page.get_by_role("tab", name=re.compile("New episodes")).click()
    expect(page.locator(".episode .ep-title")).to_have_text([re.compile(f"Episode {n}") for n in (7, 6, 5, 4)])

    page.get_by_role("button", name="Play them in turn").click()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing && m.current().title === 'Episode 7')")
    queue = page.evaluate(f"{PLAYER}.then((m) => {{ const s = m.player.get(); return {{ titles: s.order.map((i) => s.items[i].title), source: s.source }}; }})")
    assert queue == {"titles": ["Episode 7", "Episode 6", "Episode 5", "Episode 4"], "source": "New episodes"}

    show.feed_status = 503
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    page.get_by_role("button", name="Check now").click()
    expect(page.get_by_text("The last check for new episodes failed")).to_be_visible()
    page.evaluate("location.hash = '#/podcasts'")
    page.get_by_role("tab", name=re.compile("Subscriptions")).click()
    expect(page.locator(".podcast-card")).to_contain_text("Could not be checked")


def test_a_long_show_lists_fifty_episodes_and_the_rest_on_request(page, show):
    start = datetime(2025, 1, 1, 10, tzinfo=timezone.utc)
    show.episodes = [
        {"guid": f"g{n}", "title": f"Episode {n}", "path": "/ep1.mp3", "date": format_datetime(start + timedelta(days=n)), "duration": "0:00:12"} for n in range(1, 61)
    ]
    podcast_id = follow_show(page, show)
    page.evaluate(f"location.hash = '#/podcast/{podcast_id}'")
    expect(page.locator(".episode")).to_have_count(50)
    expect(page.locator(".episode").first).to_contain_text("Episode 60")
    page.get_by_role("button", name="Show more episodes").click()
    expect(page.locator(".episode")).to_have_count(60)
    expect(page.get_by_role("button", name="Show more episodes")).to_have_count(0)


# ---------------------------------------------------------------- the queue, settings, keyboard

def test_the_queue_holds_songs_stations_and_episodes_but_only_songs_are_joined_ahead(page, live, station, show):
    add_library(page, live)
    add_station(page, station)
    follow_show(page, show)
    page.evaluate(
        f"""(async () => {{
          const m = await {PLAYER};
          const songs = (await fetch('/api/tracks?limit=2').then((r) => r.json())).items;
          const stations = (await fetch('/api/radio/stations').then((r) => r.json())).items;
          const episodes = (await fetch('/api/podcasts/new').then((r) => r.json())).items;
          await m.playQueue([songs[0], songs[1], stations[0], episodes[0]], 1, {{ source: 'a mix of everything' }});
        }})()"""
    )
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing && m.deckState().decks.some((d) => d.time > 0.2))")
    page.keyboard.press("q")  # the queue
    drawer = page.locator(".queue-drawer")
    expect(drawer.locator(".qrow")).to_have_count(4)
    expect(drawer.locator(".qrow .qtime")).to_have_text([re.compile(r"0:1[12]"), re.compile(r"0:1[12]"), "LIVE", "0:12"])
    expect(drawer.get_by_text("Playing from a mix of everything")).to_be_visible()
    # The song playing is followed by a station: nothing is prepared ahead of it (a live stream would only be listened to later).
    time.sleep(2.5)
    assert state(page)["loaded"] == 1

    drawer.get_by_role("button", name="Save as playlist").click()
    page.get_by_role("dialog").get_by_label("Name").fill("Only the songs")
    page.get_by_role("dialog").get_by_role("button", name="Save").click()
    expect(page.get_by_text('Saved 2 songs as "Only the songs"')).to_be_visible()
    saved = next(p for p in api(page, "/playlists")["items"] if p["name"] == "Only the songs")
    assert saved["tracks"] == 2

    # Closing the page and opening it again brings back the songs of the queue (a station is live, an episode remembers its own place).
    page.reload()
    page.wait_for_selector(".sidebar")
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().items.length === 2)")
    assert page.evaluate(f"{PLAYER}.then((m) => m.player.get().items.map((i) => i.kind || 'track'))") == ["track", "track"]


def test_settings_can_switch_the_podcast_check_off_and_check_now(page, show):
    follow_show(page, show)
    page.evaluate("location.hash = '#/settings'")
    box = page.get_by_label("Check my podcasts for new episodes every few hours while the app is open")
    expect(box).to_be_checked()
    box.uncheck()
    wait_until(page, "fetch('/api/settings').then((r) => r.json()).then((s) => s.app.podcast_refresh === false)")
    page.get_by_role("button", name="Check for new episodes now").click()
    expect(page.get_by_text("Checking for new episodes…")).to_be_visible()
    wait_until(page, "fetch('/api/jobs').then((r) => r.json()).then((j) => j.jobs.some((x) => x.kind === 'podcasts' && x.status === 'done'))", 20)


def test_the_screens_can_be_used_from_the_keyboard(page, station):
    add_station(page, station)
    page.get_by_role("tab", name=re.compile("Favorites")).focus()
    page.keyboard.press("ArrowLeft")  # tabs: the arrow keys move between them (round the ends) and choose them
    expect(page.get_by_role("tab", name="Add by address")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Home")
    expect(page.get_by_role("tab", name=re.compile("Favorites"))).to_have_attribute("aria-selected", "true")

    page.get_by_role("button", name="Play Live FM").focus()
    page.keyboard.press("Enter")
    expect(bar(page).locator(".live-badge")).to_be_visible()
    page.get_by_role("button", name="Favorite: Live FM").focus()
    page.keyboard.press("Enter")  # the heart toggles (it was a favorite when it was added): the station leaves the favorites
    expect(page.get_by_role("heading", name="No favorite stations yet")).to_be_visible()
    page.get_by_role("button", name="Pause", exact=True).focus()  # a button: the shortcut works from anywhere but a text box
    page.keyboard.press("l")  # and "l" favorites what is playing, as it does for a song
    wait_until(page, "fetch('/api/radio/stations?scope=favorites').then((r) => r.json()).then((s) => s.counts.favorites === 1)")
    expect(page.locator(".station-card .card-title")).to_have_text("Live FM")


# ---------------------------------------------------------------- accessibility

@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_radio_and_podcast_screens_pass_an_automated_accessibility_check(page, station, directory, show, apple, theme):
    """The screens, menus and dialogs these two add, with and without something playing, in both themes (axe-core, as in test_accessibility.py)."""
    pytest.importorskip("axe_playwright_python.sync_playwright", reason="pip install -e '.[e2e]' for the accessibility checks")
    from tests.e2e.test_accessibility import CLEAR, has_a_heading, settled, violations

    page.emulate_media(reduced_motion="reduce")  # nothing is mid-fade while axe reads the colours
    page.evaluate("(theme) => import('/static/js/state.js').then((s) => s.setTheme(theme))", theme)
    add_station(page, station)
    podcast_id = follow_show(page, show)
    problems: list[str] = []

    def check(name: str, enter) -> None:
        page.evaluate(CLEAR)
        enter()
        settled(page)
        has_a_heading(page, name)
        problems.extend(f"[{name}] {line}" for line in violations(page))

    def at(hash_: str, tab: str | None = None, ready=None):
        def go() -> None:
            page.evaluate("(h) => { location.hash = h; }", hash_)
            if tab:
                page.get_by_role("tab", name=re.compile(tab)).click()
            if ready:
                expect(page.locator(ready).first).to_be_visible()

        return go

    def station_menu() -> None:
        at("#/radio", "Favorites", ".station-card")()
        page.get_by_role("button", name="More about Live FM").click()
        expect(page.locator(".menu")).to_be_visible()

    def remove_station_dialog() -> None:
        station_menu()
        page.get_by_role("menuitem", name="Remove from my stations").click()
        expect(page.get_by_role("dialog", name="Remove station")).to_be_visible()

    def episode_menu() -> None:
        at(f"#/podcast/{podcast_id}", None, ".episode")()
        page.get_by_role("button", name="More about Episode 6").click()
        expect(page.locator(".menu")).to_be_visible()

    def play_station() -> None:
        at("#/radio", "Favorites", ".station-card")()
        page.get_by_role("button", name="Play Live FM").click()
        expect(bar(page).locator(".pb-text .t1")).to_have_text("Miles Davis - So What", timeout=15_000)

    def play_episode() -> None:
        at(f"#/podcast/{podcast_id}", None, ".episode")()
        page.get_by_role("button", name="Play Episode 6").click()
        wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing && m.player.get().position > 0.5)")
        page.evaluate(f"{PLAYER}.then((m) => m.seek(3))")  # still: a row's clock would redraw it every second
        page.evaluate(f"{PLAYER}.then((m) => m.pause())")
        wait_until(page, f"{PLAYER}.then((m) => !m.player.get().playing)")

    def queue_drawer() -> None:
        page.evaluate("import('/static/js/state.js').then((s) => s.ui.set({ queueOpen: true }))")
        expect(page.locator(".queue-drawer .qrow").first).to_be_visible()

    def find_results() -> None:
        at("#/podcasts", "Find")()
        page.get_by_label("Search for a podcast").fill("test show")
        expect(page.locator(".result-card")).to_have_count(1)

    states = [
        ("radio: favorites", at("#/radio", "Favorites", ".station-card")),
        ("radio: browse", at("#/radio", "Browse", ".station-card:has-text('Second Directory Station')")),
        ("radio: recent, still empty", at("#/radio", "Recent", ".empty")),
        ("radio: add by address", at("#/radio", "Add by address", ".add-form")),
        ("a station's menu", station_menu),
        ("the remove-a-station dialog", remove_station_dialog),
        ("podcasts: subscriptions", at("#/podcasts", "Subscriptions", ".podcast-card")),
        ("podcasts: new episodes", at("#/podcasts", "New episodes", ".episode")),
        ("podcasts: find", at("#/podcasts", "Find", ".add-form")),
        ("podcasts: find, with results", find_results),
        ("a podcast", at(f"#/podcast/{podcast_id}", None, ".episode")),
        ("an episode's menu", episode_menu),
        ("radio: a station playing", play_station),
        ("now playing: a station", at("#/now", "Up next", ".qrow")),
        ("the queue with a station in it", queue_drawer),
        ("a podcast, an episode paused in the player bar", play_episode),
        ("now playing: an episode", at("#/now", "About", ".notes")),
        ("now playing: an episode, up next", at("#/now", "Up next", ".qrow")),
        ("settings", at("#/settings", None, ".card-panel")),
    ]
    for name, enter in states:
        check(name, enter)
    page.evaluate(CLEAR)
    assert not problems, f"{len(problems)} accessibility problems in the {theme} theme:\n" + "\n".join(problems)
