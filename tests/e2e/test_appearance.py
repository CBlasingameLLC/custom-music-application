"""The accent colour and the density: chosen in Settings, remembered, in place before the app is drawn, and always readable."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import add_library

expect.set_options(timeout=10_000)

APPEARANCE = "import('/static/js/appearance.js')"
ODD_COLOURS = ["#000000", "#ffffff", "#ffff00", "#0000ff", "#808080", "#12a4ff", "#ff0066", "#7a5200"]


def root_var(page, name: str) -> str:
    return page.evaluate("(name) => document.documentElement.style.getPropertyValue(name).trim()", name)


def open_settings(page) -> None:
    page.evaluate("location.hash = '#/settings'")
    expect(page.get_by_role("heading", name="Appearance", exact=True)).to_be_visible()


def density_choice(page):
    # Not get_by_label("Density"): that matches by substring, and a button's label can hold the test's own folder name.
    return page.locator("label.field", has_text="Density").locator("select")


def test_every_accent_is_readable_in_both_themes_and_so_is_any_colour_someone_picks(page) -> None:
    result = page.evaluate(
        f"""{APPEARANCE}.then((a) => {{
          const rows = [];
          for (const accent of [...a.ACCENTS.map((x) => x.id), {json.dumps(ODD_COLOURS)}].flat()) {{
            for (const theme of ['dark', 'light']) {{
              const c = a.accentContrasts(accent, theme);
              rows.push({{ accent, theme, text: c.text, button: c.button, hover: c.hover }});
            }}
          }}
          return {{ rows, min: a.MIN_CONTRAST, presets: a.ACCENTS.length }};
        }})"""
    )

    assert result["presets"] == 8 and len(result["rows"]) == (8 + len(ODD_COLOURS)) * 2
    too_dim = [r for r in result["rows"] if min(r["text"], r["button"], r["hover"]) < result["min"]]
    assert not too_dim, too_dim


def test_accent_text_reads_on_every_surface_also_with_a_selected_rows_tint_or_the_pointer_over_it(page) -> None:
    # Measured against the surfaces the stylesheet really has, and the tints it really lays over them (not the ones the
    # derivation assumes): the tint of a selected row is the accent itself at --accent-soft's opacity, the pointer's is --bg-active.
    result = page.evaluate(
        f"""{APPEARANCE}.then(async (a) => {{
          const state = await import('/static/js/state.js');
          const root = document.documentElement;
          const rgb = (text) => text.match(/\\d+(\\.\\d+)?/g).map(Number);
          const mix = (over, under) => under.map((u, i) => over[i] * over[3] + u * (1 - over[3]));
          const rows = [];
          for (const theme of ['dark', 'light']) {{
            state.setTheme(theme);
            for (const accent of [...a.ACCENTS.map((x) => x.id), ...{json.dumps(ODD_COLOURS)}]) {{
              state.setAccent(accent);
              const style = getComputedStyle(root);
              const surfaces = ['--bg', '--bg-sidebar', '--bg-elev', '--bg-elev-2'].map((name) => a.parseHex(style.getPropertyValue(name).trim()));
              const soft = rgb(style.getPropertyValue('--accent-soft'));
              const press = rgb(style.getPropertyValue('--bg-active'));
              const text = a.parseHex(style.getPropertyValue('--accent').trim());
              const grounds = surfaces.flatMap((s) => [s, mix(soft, s), mix(press, s)]);
              rows.push({{ theme, accent, worst: Math.min(...grounds.map((g) => a.contrast(text, g))) }});
            }}
          }}
          return rows;
        }})"""
    )

    assert len(result) == 2 * (8 + len(ODD_COLOURS))
    assert [r for r in result if r["worst"] < 4.5] == []


def test_nonsense_colours_fall_back_to_the_default(page) -> None:
    result = page.evaluate(
        f"""{APPEARANCE}.then((a) => ['nonsense', null, '#12345', 'javascript:alert(1)', 42].map((x) => a.normalizeAccent(x)))"""
    )
    assert result == ["violet"] * 5


def test_choosing_an_accent_in_settings_changes_the_page_and_is_remembered(page) -> None:
    open_settings(page)
    teal = page.get_by_role("button", name="Teal")
    expect(teal).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_role("button", name="Violet")).to_have_attribute("aria-pressed", "true")

    teal.click()

    expect(teal).to_have_attribute("aria-pressed", "true")
    assert root_var(page, "--accent") == "#2bb8a8" and root_var(page, "--on-accent") == "#0b0d12"
    saved = page.evaluate("JSON.parse(localStorage.getItem('mtk.ui.v1'))")
    assert saved["accent"] == "teal" and saved["vars"]["--accent"] == "#2bb8a8"
    page.reload()
    page.wait_for_selector(".sidebar")
    assert root_var(page, "--accent") == "#2bb8a8"


def test_the_saved_look_is_in_place_before_the_app_has_even_started(page) -> None:
    open_settings(page)
    page.get_by_role("button", name="Rose").click()
    page.get_by_role("button", name="Rose").evaluate("() => { const s = JSON.parse(localStorage.getItem('mtk.ui.v1')); s.theme = 'light'; localStorage.setItem('mtk.ui.v1', JSON.stringify(s)); }")
    page.route("**/static/js/main.js", lambda route: route.fulfill(status=200, content_type="text/javascript", body=""))  # the app itself never starts

    page.reload()

    assert page.evaluate("document.documentElement.dataset.theme") == "light", "the saved theme is on the page before any app code runs"
    assert root_var(page, "--accent") != "", "and so is the saved accent"
    assert page.evaluate("document.documentElement.dataset.density") == "comfortable"
    page.unroute("**/static/js/main.js")


def test_any_colour_can_be_chosen_and_the_derived_colours_stay_readable(page) -> None:
    open_settings(page)

    page.get_by_label("Custom accent colour").evaluate(
        "el => { el.value = '#ffee00'; el.dispatchEvent(new Event('input', { bubbles: true })); }"
    )

    saved = page.evaluate("JSON.parse(localStorage.getItem('mtk.ui.v1'))")
    assert saved["accent"] == "#ffee00"
    assert root_var(page, "--on-accent") == "#0b0d12", "dark text on a yellow button"
    ratio = page.evaluate(f"{APPEARANCE}.then((a) => a.contrast(document.documentElement.style.getPropertyValue('--accent-fill').trim(), document.documentElement.style.getPropertyValue('--on-accent').trim()))")
    assert ratio >= 4.5
    expect(page.locator(".swatch.custom.on")).to_have_count(1)


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("accent", ["violet", "blue", "amber", "graphite"])
def test_the_text_on_a_primary_button_is_readable_as_drawn_also_under_the_pointer(page, accent, theme) -> None:
    page.emulate_media(reduced_motion="reduce")  # colours arrive at once instead of easing to their new value
    page.evaluate("([a, t]) => import('/static/js/state.js').then((s) => { s.setAccent(a); s.setTheme(t); })", [accent, theme])
    page.evaluate("location.hash = '#/'")
    button = page.locator(".btn.primary").first
    expect(button).to_be_visible()

    def ratio() -> float:
        return button.evaluate(
            f"""(el) => {APPEARANCE}.then((a) => {{
              const style = getComputedStyle(el);
              const rgb = (text) => text.match(/\\d+(\\.\\d+)?/g).slice(0, 3).map(Number);
              return a.contrast(rgb(style.backgroundColor), rgb(style.color));
            }})"""
        )

    page.mouse.move(0, 0)
    resting = ratio()
    button.hover()
    hovered = ratio()

    assert resting >= 4.5, f"{accent} in the {theme} theme: {resting:.2f}"
    assert hovered >= 4.5, f"{accent} in the {theme} theme under the pointer: {hovered:.2f}"


def test_compact_density_shortens_the_song_rows_and_the_table_still_lines_up(page, live) -> None:
    add_library(page, live)
    heights = lambda: page.evaluate("Array.from(document.querySelectorAll('.trow:not(.skeleton)'), (r) => [r.getBoundingClientRect().height, r.offsetTop])")  # noqa: E731
    comfortable = heights()
    assert len(comfortable) == 9 and {h for h, _ in comfortable} == {52}
    assert [top for _, top in comfortable] == [52 * n for n in range(9)]

    open_settings(page)
    density_choice(page).select_option("compact")
    assert page.evaluate("document.documentElement.dataset.density") == "compact"
    page.evaluate("location.hash = '#/songs'")
    page.wait_for_selector(".trow:not(.skeleton)")

    compact = heights()
    assert len(compact) == 9 and {h for h, _ in compact} == {40}, compact
    assert [top for _, top in compact] == [40 * n for n in range(9)], "rows sit edge to edge, none overlapping or gapped"
    assert page.evaluate("document.querySelector('.tbody').getBoundingClientRect().height") == 9 * 40
    page.reload()
    page.wait_for_selector(".trow:not(.skeleton)")
    assert {h for h, _ in heights()} == {40}, "remembered"


def test_density_and_accent_survive_switching_theme(page) -> None:
    open_settings(page)
    page.get_by_role("button", name="Blue").click()
    density_choice(page).select_option("compact")

    page.get_by_role("button", name="Switch to light theme").click()

    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    assert page.evaluate("document.documentElement.dataset.density") == "compact"
    light_accent = root_var(page, "--accent")
    assert light_accent != "#4f8dff", "a lighter page needs a darker accent to stay readable"
    assert page.evaluate("JSON.parse(localStorage.getItem('mtk.ui.v1')).accent") == "blue"
