"""The Stats screen in a real browser: the figures, the charts, hover and keyboard, the table twin, periods, both themes.

Set MTK_SHOTS to a folder to also get a screenshot of the main states (for looking at, not asserted).
"""

from __future__ import annotations

import calendar
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import expect

from tests.e2e.conftest import add_library

expect.set_options(timeout=15_000)

MS = 4 * 60_000  # every play lasted four minutes


def day(ago: int) -> datetime:
    """Midnight (UTC, the time zone of the test browser) `ago` days before today."""
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=ago)


def seed_history(live) -> list[tuple[int, int, str, str]]:
    """44 plays inside the last twelve months on 39 different days (day 5 is the busy one), so the grid has exactly as
    many lit squares as there are days. Returns (days ago, hour, artist, title) for each."""
    plays: list[tuple[int, int, str, str]] = []
    for window in range(6):
        for k in range(3):
            plays.append((5 + window * 30 + k * 3, 21, "Aurora Vale", "Glacier"))
    for window in range(8):
        for k in range(2):
            plays.append((12 + window * 30 + k * 5, 8, "The Fernwoods", "Gravel Dust"))
    plays += [(300, 13, "Mara Quinn", "Wires"), (301, 13, "Mara Quinn", "Wires"), (302, 13, "Mara Quinn", "Wires"), (20, 13, "Mara Quinn", "Wires")]
    plays.append((2, 23, "Spotify Only Artist", "A Song You Never Owned"))  # a play of a song that is not in the library
    assert len({p[0] for p in plays}) == len(plays), "apart from the busy day, one play per day"
    plays += [(5, 20, "Aurora Vale", "Glacier")] * 3 + [(5, 9, "The Fernwoods", "Gravel Dust")] * 2  # the busy day

    with live.ctx.db() as conn:
        ids = {r["title"]: r["id"] for r in conn.execute("SELECT id, title FROM tracks")}
        for n, (ago, hour, artist, title) in enumerate(plays):
            when = int((day(ago) + timedelta(hours=hour, seconds=n)).timestamp())
            conn.execute(
                "INSERT INTO play_history (source, played_at_epoch, ms_played, track_id, raw_artist_name, raw_track_name) "
                "VALUES ('future_scrobble', ?, ?, ?, ?, ?)",
                (when, MS, ids.get(title), artist, title),
            )
        conn.commit()
    return plays


def open_stats(page) -> None:
    page.get_by_role("link", name="Stats", exact=True).click()
    expect(page.get_by_role("heading", name="Stats", exact=True)).to_be_visible()


def tile(page, label: str):
    """The figure of the tile with this name."""
    return page.locator(".stat-tile", has=page.get_by_text(label, exact=True)).locator("dd").first


def card(page, title: str):
    return page.locator("figure.viz-card", has=page.get_by_role("heading", name=title, exact=True))


def stats_with_history(page, live) -> list[tuple[int, int, str, str]]:
    add_library(page, live)
    plays = seed_history(live)
    page.evaluate("location.hash = '#/stats'")
    expect(page.locator(".big")).to_be_visible()
    return plays


def shoot(page, name: str) -> None:
    folder = os.environ.get("MTK_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"))


# ------------------------------------------------------------------------------------------- the figures


def test_an_empty_history_points_to_the_import(page, live):
    open_stats(page)

    expect(page.get_by_role("heading", name="No listening history yet")).to_be_visible()
    page.get_by_role("button", name="Import Spotify history").click()
    expect(page.get_by_role("heading", name="Import Spotify history", exact=True)).to_be_visible()


def test_the_last_twelve_months_are_counted(page, live):
    plays = stats_with_history(page, live)
    minutes = len(plays) * MS // 60_000

    expect(page.get_by_label("Period")).to_have_value("days:365")
    expect(page.locator(".big")).to_have_text(f"{round(minutes / 60, 1):g}")  # 44 plays of four minutes: 2.9 hours
    expect(page.locator(".big-unit")).to_have_text("hours of music")
    expect(tile(page, "Plays")).to_have_text("44")
    expect(tile(page, "Artists")).to_have_text("4")
    expect(tile(page, "Songs")).to_have_text("4")
    expect(tile(page, "Days with music")).to_have_text(str(len({p[0] for p in plays})))
    expect(tile(page, "Longest streak")).to_contain_text("3 days")
    expect(tile(page, "New artists")).to_have_text("4")
    expect(page.locator(".stat-tile", has_text="New artists").locator("dd.note")).to_have_text("not played before")

    artists = card(page, "Top artists").locator("li")
    expect(artists).to_have_count(4)
    expect(artists.nth(0)).to_contain_text("Aurora Vale")
    expect(artists.nth(0).locator(".rk-val")).to_have_text("21")
    expect(artists.nth(1)).to_contain_text("The Fernwoods")
    expect(artists.nth(1).locator(".rk-val")).to_have_text("18")
    songs = card(page, "Top songs").locator("li")
    expect(songs.nth(0)).to_contain_text("Glacier")
    expect(songs.nth(0)).to_contain_text("Aurora Vale")
    expect(card(page, "Top albums").locator("li").nth(0)).to_contain_text("Northern Lights")
    expect(card(page, "Top genres").locator("li").nth(0)).to_contain_text("Electronic")
    expect(card(page, "Song of each month")).to_be_visible()
    expect(card(page, "Artists you found").locator("li")).to_have_count(4)
    shoot(page, "stats-dark")


def test_the_top_of_the_page_says_who_what_and_when(page, live):
    plays = stats_with_history(page, live)
    by_weekday = Counter(day(p[0]).weekday() for p in plays)
    by_hour = Counter(p[1] for p in plays)
    busiest_weekday = min(range(7), key=lambda d: (-by_weekday[d], d))  # on a tie the earlier day, as the page does
    busiest_hour = min(range(24), key=lambda h: (-by_hour[h], h))

    highlights = page.get_by_role("list", name="Highlights")
    expect(highlights.locator("li", has_text="Most played artist")).to_contain_text("Aurora Vale")
    expect(highlights.locator("li", has_text="Most played song")).to_contain_text("Glacier")
    expect(highlights.locator("li", has_text="Busiest day")).to_contain_text(calendar.day_name[busiest_weekday])
    expect(highlights.locator("li", has_text="Busiest hour")).to_contain_text(f"{busiest_hour % 12 or 12} {'AM' if busiest_hour < 12 else 'PM'}")
    expect(page.get_by_role("heading", name="When you listen")).to_be_visible()
    expect(page.get_by_role("heading", name="What you listen to")).to_be_visible()


def test_a_square_for_each_day_with_a_tooltip_for_the_pointer_and_the_keyboard(page, live):
    plays = stats_with_history(page, live)

    grid = card(page, "Days you listened")
    expect(grid.locator("rect.cell:not(.lv0)")).to_have_count(len({p[0] for p in plays}))
    expect(grid.locator("rect.cell.lv4")).to_have_count(1)  # the busy day alone is in the top quarter

    plot = grid.locator(".viz-plot")
    tip = grid.locator(".viz-tip")
    plot.focus()
    plot.press("End")
    expect(tip).to_be_visible()  # today
    plot.press("Home")
    expect(tip).to_contain_text(" play")
    plot.press("Escape")
    expect(tip).to_have_count(0)

    grid.locator("rect.cell.lv4").scroll_into_view_if_needed()
    box = grid.locator("rect.cell.lv4").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    busy = day(5)
    expect(tip).to_contain_text("6 plays")
    expect(tip).to_contain_text(f"{busy:%b} {busy.day}, {busy.year}")
    shoot(page, "stats-heatmap-hover")


def test_a_column_chart_answers_the_pointer_and_the_arrow_keys(page, live):
    plays = stats_with_history(page, live)
    newest = day(min(p[0] for p in plays))
    earlier = newest.replace(day=1) - timedelta(days=1)

    def plays_in(month: datetime) -> int:
        return sum(1 for p in plays if day(p[0]).strftime("%Y-%m") == month.strftime("%Y-%m"))

    months = card(page, "Plays by month")
    plot = months.locator(".viz-plot")
    tip = months.locator(".viz-tip")
    plot.scroll_into_view_if_needed()
    box = plot.bounding_box()
    page.mouse.move(box["x"] + box["width"] - 14, box["y"] + 60)
    expect(tip).to_contain_text(newest.strftime("%B %Y"))
    expect(tip).to_contain_text(f"{plays_in(newest)} play")
    shoot(page, "stats-month-hover")

    page.mouse.move(box["x"] + box["width"] / 2, 2)  # off the chart
    expect(tip).to_have_count(0)

    plot.focus()
    plot.press("End")
    expect(tip).to_contain_text(newest.strftime("%B %Y"))
    plot.press("ArrowLeft")
    expect(tip).to_contain_text(earlier.strftime("%B %Y"))
    expect(tip).to_contain_text(f"{plays_in(earlier)} play")
    expect(plot.locator("[role=status]")).to_contain_text(earlier.strftime("%B %Y"))
    plot.press("Escape")
    expect(tip).to_have_count(0)


def test_a_chart_with_one_play_says_play_not_plays(page, live):
    stats_with_history(page, live)
    plot = card(page, "Time of day").locator(".viz-plot")  # 23 o'clock has the one play of the song that is not in the library

    plot.focus()
    plot.press("End")

    expect(card(page, "Time of day").locator(".viz-tip strong")).to_have_text("1 play")


def test_the_time_of_day_and_week_charts_have_a_tooltip_for_each_column(page, live):
    stats_with_history(page, live)

    expect(card(page, "Time of day").locator(".viz-bars .bar")).to_have_count(6)  # plays at 8, 9, 13, 20, 21 and 23 o'clock
    weekdays = card(page, "Day of the week").locator(".viz-plot")
    tip = card(page, "Day of the week").locator(".viz-tip")
    weekdays.focus()
    weekdays.press("Home")
    expect(tip).to_contain_text("Monday")
    weekdays.press("End")
    expect(tip).to_contain_text("Sunday")


def test_every_chart_has_a_table_with_the_same_numbers(page, live):
    plays = stats_with_history(page, live)
    months_with_plays = {day(p[0]).strftime("%Y-%m") for p in plays}

    months = card(page, "Plays by month")
    switch = months.get_by_role("button", name="Table")
    expect(switch).to_have_attribute("aria-pressed", "false")
    switch.click()

    expect(switch).to_have_attribute("aria-pressed", "true")
    expect(months.locator("svg")).to_have_count(0)
    table = months.get_by_role("table", name="Plays by month")
    assert table.locator("tbody tr").count() >= len(months_with_plays), "a month without plays between two with plays is listed too"
    assert sum(int(c) for c in table.locator("tbody td:nth-child(2)").all_inner_texts()) == len(plays)
    switch.click()
    expect(months.locator("svg")).to_have_count(1)

    top = card(page, "Top songs")
    top.get_by_role("button", name="Table").click()
    expect(top.get_by_role("table")).to_contain_text("Glacier by Aurora Vale")

    for title in ("Time of day", "Day of the week", "Days you listened"):
        card(page, title).get_by_role("button", name="Table").click()
        assert sum(int(c) for c in card(page, title).locator("tbody td:nth-child(2)").all_inner_texts()) == len(plays), title


# ------------------------------------------------------------------------------------------- periods


def test_another_period_keeps_the_old_figures_on_screen_until_the_new_ones_arrive(page, live):
    plays = stats_with_history(page, live)
    expect(tile(page, "Plays")).to_have_text("44")

    held = []
    page.route("**/api/stats/overview*", lambda route: held.append(route))
    page.get_by_label("Period").select_option("all")
    expect(page.locator(".stats.refreshing")).to_be_visible()
    expect(tile(page, "Plays")).to_have_text("44")  # still the last twelve months, dimmed, with no spinner in their place
    expect(page.locator(".view .spinner")).to_have_count(0)
    held[0].continue_()
    page.unroute("**/api/stats/overview*")

    expect(page.locator(".stats.refreshing")).to_have_count(0)
    expect(page.locator(".page-header .subtle")).to_have_text("All time")
    expect(tile(page, "Plays")).to_have_text(str(len(plays)))
    expect(tile(page, "New artists")).to_have_count(0)  # over all time every artist is new
    expect(page.get_by_role("heading", name="Days you listened")).to_have_count(0)  # a grid of days only for up to a year
    expect(page.get_by_role("heading", name="Plays by month")).to_be_visible()


def test_a_short_period_shows_a_column_for_each_day(page, live):
    plays = stats_with_history(page, live)

    page.get_by_label("Period").select_option("days:30")

    recent = [p for p in plays if p[0] < 30]
    expect(tile(page, "Plays")).to_have_text(str(len(recent)))
    daily = card(page, "Plays each day")
    expect(daily.locator(".viz-bars .bar")).to_have_count(len({p[0] for p in recent}))
    expect(page.get_by_role("heading", name="Days you listened")).to_have_count(0)
    expect(page.get_by_role("heading", name="Plays by month")).to_have_count(0)  # two months is not a trend; the days already show it
    labels = daily.locator(".viz-axis-labels text").all_text_contents()
    assert re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}", labels[0]), f"the first column says which month it is in: {labels[:3]}"
    assert all(re.fullmatch(r"([A-Z][a-z]{2} )?\d{1,2}", label) for label in labels), labels
    shoot(page, "stats-30-days")


def test_a_calendar_year_can_be_chosen(page, live):
    plays = stats_with_history(page, live)
    year = datetime.now(timezone.utc).year

    page.get_by_label("Period").select_option(f"year:{year}")

    expect(page.locator(".page-header .subtle")).to_have_text(str(year))
    expect(tile(page, "Plays")).to_have_text(str(sum(1 for p in plays if day(p[0]).year == year)))


def test_a_history_that_stopped_years_ago_opens_on_its_latest_year(page, live):
    with live.ctx.db() as conn:
        conn.execute(
            "INSERT INTO play_history (source, played_at_epoch, ms_played, raw_artist_name, raw_track_name) VALUES ('spotify_import', ?, ?, 'Old Band', 'Old Song')",
            (int(datetime(2019, 6, 1, 12, tzinfo=timezone.utc).timestamp()), MS),
        )
        conn.commit()
    page.evaluate("location.hash = '#/stats'")

    expect(page.get_by_label("Period")).to_have_value("year:2019")
    expect(page.locator(".page-header .subtle")).to_have_text("2019")
    expect(page.locator(".big")).to_have_text("4")
    expect(page.locator(".big-unit")).to_have_text("minutes of music")

    page.get_by_label("Period").select_option("days:30")
    expect(page.get_by_role("heading", name="No plays in the last 30 days")).to_be_visible()


# ------------------------------------------------------------------------------------------- playing and linking


def test_a_top_song_plays_and_a_song_you_do_not_own_does_not(page, live):
    stats_with_history(page, live)
    songs = card(page, "Top songs")

    expect(songs.get_by_text("A Song You Never Owned")).to_be_visible()
    expect(songs.get_by_role("button", name="A Song You Never Owned")).to_have_count(0)
    songs.get_by_role("button", name="Glacier", exact=True).click()

    expect(page.locator(".playerbar .pb-text .t1")).to_have_text("Glacier")


def test_an_artist_in_the_list_leads_to_a_search_for_it(page, live):
    stats_with_history(page, live)

    card(page, "Top artists").get_by_role("link", name="The Fernwoods").click()

    expect(page).to_have_url(re.compile(r"#/search/The%20Fernwoods$"))


# ------------------------------------------------------------------------------------------- colour


def fill(page, selector: str) -> str:
    """The colour a mark has once any transition has run its course."""
    page.wait_for_timeout(0)
    previous = None
    for _ in range(40):
        current = page.evaluate("(s) => getComputedStyle(document.querySelector(s)).fill", selector)
        if current == previous:
            return current
        previous = current
        page.wait_for_timeout(50)
    return previous


def test_the_chart_colours_follow_the_theme(page, live):
    stats_with_history(page, live)
    expect(page.locator(".viz-bars .bar").first).to_be_visible()
    page.evaluate("document.documentElement.dataset.theme = 'dark'")

    assert fill(page, ".viz-bars .bar") == "rgb(57, 135, 229)"  # #3987e5
    assert fill(page, "rect.cell.lv4") == "rgb(183, 211, 246)", "on a dark page more plays is lighter"
    assert fill(page, "rect.cell.lv1") == "rgb(24, 79, 149)"
    assert fill(page, "rect.cell.lv0") != fill(page, "rect.cell.lv1"), "a day without plays is not a day with few"

    page.evaluate("document.documentElement.dataset.theme = 'light'")

    assert fill(page, ".viz-bars .bar") == "rgb(42, 120, 214)"  # #2a78d6
    assert fill(page, "rect.cell.lv4") == "rgb(13, 54, 107)", "on a light page more plays is darker"
    assert fill(page, "rect.cell.lv1") == "rgb(134, 182, 239)"
    shoot(page, "stats-light")
