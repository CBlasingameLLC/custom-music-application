"""A file that is only tags, in the interface: marked, left out of what plays, and said so when one is asked for."""

from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.conftest import make_stub
from tests.e2e.conftest import play, row

expect.set_options(timeout=10_000)


def test_a_file_that_is_only_tags_is_marked_refused_in_words_and_left_out_of_what_plays_on(page, live):
    make_stub(live.library / "Stubs" / "Empty Album", "01 - Hidden Song.mp3")
    asked: list[str] = []
    page.on("request", lambda r: asked.append(r.url) if "/stream" in r.url else None)
    page.get_by_role("button", name="Add music folder").click()  # (the way a person adds it: the toast about the scan is the one to read)
    page.get_by_label("Folder path").fill(str(live.library))
    page.get_by_role("button", name="Choose").click()
    expect(page.get_by_text("Library updated: 10 added, 0 changed, 0 missing. 1 file has tags but no audio.")).to_be_visible(timeout=60_000)
    page.get_by_role("link", name="Songs", exact=True).click()
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(10)

    stub = row(page, "Hidden Song")
    expect(stub).to_have_class(re.compile(r"\bno-audio\b"))
    expect(stub.locator(".badge")).to_have_text("No audio")
    expect(stub.locator(".badge")).to_have_attribute("title", re.compile("nothing to play"))
    expect(row(page, "Drift")).not_to_have_class(re.compile(r"\bno-audio\b"))
    expect(page.locator(".trow .badge")).to_have_count(1)

    play(page, "Hidden Song")  # (asking for it says why not, and asks the server for nothing)
    expect(page.locator(".toast.error")).to_contain_text("Can't play \"Hidden Song\": it has tags but no audio in it.")
    assert asked == []
    expect(page.locator(".playerbar .pb-text .t1")).to_have_count(0)

    titles = [t.strip() for t in page.locator(".trow:not(.skeleton) .t1").all_inner_texts()]
    titles = [t.replace("No audio", "").strip() for t in titles]
    after = titles[titles.index("Hidden Song") + 1]  # (the song under the one that is only tags: it has to be the one that plays, not the one after it)
    play(page, after)
    expect(page.locator(".playerbar .pb-text .t1")).to_have_text(after)

    stub.click(button="right")  # (queueing it says that it cannot be, and queues nothing)
    page.get_by_role("menuitem", name="Add to queue").click()
    expect(page.locator(".toast", has_text='"Hidden Song" has tags but no audio, so there is nothing to queue')).to_be_visible()
    page.get_by_role("link", name="Songs", exact=True).click()

    first = page.locator(".trow:not(.skeleton) .t1").first.inner_text()
    play(page, first)  # (playing the list goes on past the file that is only tags: it is not in the queue)
    bar = page.locator(".playerbar .pb-text .t1")
    expect(bar).to_have_text(first)
    heard = [first]
    for _ in range(8):
        before = heard[-1]
        page.locator(".playerbar").get_by_role("button", name="Next").click()
        expect(bar).not_to_have_text(before)
        heard.append(bar.inner_text())
    assert "Hidden Song" not in heard and len(set(heard)) == 9
    assert asked and all(url.endswith("/stream") for url in asked)
    stub_id = page.evaluate("fetch('/api/tracks?limit=50').then(r => r.json()).then(d => d.items.find(t => t.title === 'Hidden Song').id)")
    assert not any(f"/tracks/{stub_id}/" in url for url in asked)


def test_an_album_of_nothing_but_files_without_audio_queues_nothing_and_is_not_started(page, live):
    make_stub(live.library / "Stubs" / "Empty Album", "01 - Hidden Song.mp3", album="Only Tags")
    page.get_by_role("button", name="Add music folder").click()
    page.get_by_label("Folder path").fill(str(live.library))
    page.get_by_role("button", name="Choose").click()
    expect(page.get_by_text("1 file has tags but no audio.")).to_be_visible(timeout=60_000)
    page.get_by_role("link", name="Albums", exact=True).click()
    page.get_by_text("Only Tags").first.click()
    expect(page.locator(".hero h1")).to_have_text("Only Tags")
    page.get_by_role("button", name="Add to queue").click()
    page.get_by_role("button", name="Play", exact=True).first.click()
    expect(page.locator(".playerbar .pb-text .t1")).to_have_count(0)  # (nothing was queued, and nothing started)
