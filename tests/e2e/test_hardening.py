"""Things that must stay true however hostile the text the page is given."""

from __future__ import annotations

from playwright.sync_api import expect

expect.set_options(timeout=15_000)


def test_an_icon_title_is_shown_as_text_never_run_as_markup(page, live):
    result = page.evaluate(
        """async () => {
            const { html, render } = await import('/static/js/lib.js');
            const { Icon } = await import('/static/js/icons.js');
            const host = document.createElement('div');
            const hostile = '</title><img src="/nowhere.png" onerror="window.__pwned = 1">';
            render(html`<${Icon} name="clock" title=${hostile} />`, host);
            document.body.appendChild(host);
            return { images: host.querySelectorAll('img').length, title: host.querySelector('svg title')?.textContent, hostile };
        }"""
    )

    assert result["images"] == 0, "the title became an element"
    assert result["title"] == result["hostile"], "and it still reads exactly as given"
