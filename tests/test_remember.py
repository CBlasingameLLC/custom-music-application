"""The key that gets a device its sign-in back when its browser has lost the cookie (remember.py), the way the app hands it out and takes it, what the page that
asks for the token says about the request it answers, and what the cookie being Lax asks of /api: a request that a browser says another site made is not answered,
whatever cookie it carries."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import html
import logging
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from musictoolkit.web import remember, signin
from musictoolkit.web.signin import arrival, sign_in_page
from tests.conftest import build_web

TOKEN = "k" * 8 + "T0ken" + "z" * 30
DAY = 86400
NOW = 1_800_000_000
YOU = "you@example.com"
ID = {"tailscale-user-login": YOU}
DOCUMENT = {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}  # (what a browser says of a page opened from an icon)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _web(tmp_path: Path, **options):
    options.setdefault("login_days", 90)
    web = build_web(tmp_path, None, token=TOKEN, **options)
    web.clock = Clock()
    web.app.state.limiter._clock = web.clock
    return web


def _device(web) -> TestClient:
    """Another device on the same app: its own cookies."""
    return TestClient(web.app, raise_server_exceptions=False)


def _signed_in(web) -> TestClient:
    device = _device(web)
    assert device.get(f"/?token={TOKEN}").status_code == 200
    return device


def _key() -> str:
    return remember.make(TOKEN, int(time.time()) + 30 * DAY)


# ---------------------------------------------------------------------------------------------------- the key


def test_a_key_is_made_from_the_token_and_the_second_it_ends() -> None:
    key = remember.make(TOKEN, NOW + 90 * DAY)
    assert re.fullmatch(r"r1\.[0-9]{10}\.[A-Za-z0-9_-]{43}", key) and TOKEN not in key
    assert key == remember.make(TOKEN, NOW + 90 * DAY)  # (made again, it is the same: nothing is kept anywhere)
    assert key != remember.make(TOKEN, NOW + 90 * DAY + 1) and key != remember.make(TOKEN + "x", NOW + 90 * DAY)
    assert remember.make("tökén-\ud800", NOW + DAY).startswith("r1.")  # (a token that is not plain ASCII, or not even text, is made into a key too)


def test_a_key_is_good_until_the_second_it_ends_and_never_when_it_says_longer_than_a_key_can() -> None:
    assert remember.valid(remember.make(TOKEN, NOW + 1), TOKEN, NOW) is True
    assert remember.valid(remember.make(TOKEN, NOW), TOKEN, NOW) is False  # (the second it ends it is no longer good)
    assert remember.valid(remember.make(TOKEN, NOW - 1), TOKEN, NOW) is False
    assert remember.valid(remember.make(TOKEN, NOW + 400 * DAY), TOKEN, NOW) is True  # (400 days: what a browser keeps a cookie for at most)
    assert remember.valid(remember.make(TOKEN, NOW + 400 * DAY + 1), TOKEN, NOW) is False  # (made when a clock was wrong)
    assert remember.LONGEST == 400 * DAY


def test_a_key_longer_than_a_key_can_be_is_refused_before_a_mac_is_made_of_it(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[int] = []
    real = remember.make

    def spy(token: str, expires: int) -> str:
        made.append(expires)
        return real(token, expires)

    monkeypatch.setattr(remember, "make", spy)
    start = f"r1.{NOW + DAY}."
    assert remember.MOST_CHARACTERS == 120
    assert remember.valid(start + "A" * (121 - len(start)), TOKEN, NOW) is False and made == []  # (121 characters: not looked at)
    assert remember.valid(start + "A" * (120 - len(start)), TOKEN, NOW) is False and made == [NOW + DAY]  # (120: the MAC is made, and it is not the key's)
    assert len(remember.make(TOKEN, NOW + DAY)) <= remember.MOST_CHARACTERS


def test_a_key_is_good_for_the_token_it_was_made_from_and_no_other() -> None:
    key = remember.make(TOKEN, NOW + DAY)
    assert remember.valid(key, TOKEN, NOW) is True
    assert remember.valid(key, TOKEN + "x", NOW) is False and remember.valid(key, "", NOW) is False


def test_the_mac_is_made_for_this_use_only() -> None:
    """The token signs other things too (the cookie of a Tailscale sign-in): a MAC made without this use's label is no key."""
    mac = hmac.new(TOKEN.encode(), str(NOW + DAY).encode(), hashlib.sha256).digest()
    plain = f"r1.{NOW + DAY}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    assert remember.valid(plain, TOKEN, NOW) is False
    labelled = hmac.new(TOKEN.encode(), remember.LABEL + str(NOW + DAY).encode(), hashlib.sha256).digest()
    assert remember.valid(f"r1.{NOW + DAY}.{base64.urlsafe_b64encode(labelled).rstrip(b'=').decode()}", TOKEN, NOW)


def test_the_mac_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []
    real = signin.same_token

    def spy(given: str, token: str) -> bool:
        seen.append((given, token))
        return real(given, token)

    monkeypatch.setattr(remember, "same_token", spy)
    key = remember.make(TOKEN, NOW + DAY)
    assert remember.valid(key, TOKEN, NOW) and seen == [(key, key)]  # (the key given, against the one made again)


GOOD = remember.make(TOKEN, NOW + DAY)
SECOND = str(NOW + DAY)


@pytest.mark.parametrize(
    "key",
    [
        GOOD[:-1] + ("A" if GOOD[-1] != "A" else "B"),  # (the last character of the MAC changed)
        GOOD.replace(SECOND, str(NOW + 2 * DAY)),  # (a later end, with the MAC of the earlier)
        GOOD + "A",
        GOOD[:-1],
        " " + GOOD,
        GOOD + " ",
        GOOD.replace("r1.", "R1.", 1),
        GOOD.replace("r1.", "r2.", 1),
        GOOD.replace("r1.", "", 1),
        GOOD + ".x",
        GOOD.replace("." + SECOND + ".", ".+" + SECOND[1:] + ".", 1),  # (a sign where a digit is)
        GOOD.replace(SECOND, "١" * 10, 1),  # (digits that are not 0-9)
        GOOD.replace(SECOND, "", 1),
        GOOD.replace(SECOND, "9" * 12, 1),
        "r1." + SECOND + "." + "A" * 200,  # (longer than a key is)
        "r1.",
        ".",
        "",
        "r1.\ud800.\ud800",
    ],
)
def test_a_key_that_was_changed_or_is_not_a_key_is_refused(key: str) -> None:
    assert remember.valid(key, TOKEN, NOW) is False


def test_what_is_not_text_is_refused_without_a_fault() -> None:
    assert remember.valid(None, TOKEN, NOW) is False  # type: ignore[arg-type]
    assert remember.valid(b"r1.1.2", TOKEN, NOW) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------------- handing out the key


def test_a_device_that_signed_in_with_the_token_is_given_a_key_that_ends_when_its_cookie_would(tmp_path: Path) -> None:
    device = _signed_in(_web(tmp_path))
    before = time.time()
    answer = device.post("/api/session/remember")
    after = time.time()
    assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
    body = answer.json()
    assert set(body) == {"key", "expires", "days"} and body["days"] == 90
    assert before + 90 * DAY - 1 <= body["expires"] <= after + 90 * DAY + 1
    assert body["key"] == remember.make(TOKEN, body["expires"]) and remember.valid(body["key"], TOKEN, time.time())


def test_a_device_that_is_not_signed_in_is_given_no_key(tmp_path: Path) -> None:
    assert _device(_web(tmp_path)).post("/api/session/remember").status_code == 401


def test_a_program_with_the_token_is_given_no_key_and_neither_is_a_device_tailscale_signed_in(tmp_path: Path) -> None:
    web = _web(tmp_path, tailscale_logins=[YOU])
    program = _device(web).post("/api/session/remember", headers={"x-mtk-token": TOKEN})
    assert program.status_code == 403 and program.json() == {"detail": "only a device that is signed in with the token keeps a key"}
    by_tailscale = _device(web)
    assert by_tailscale.get("/", headers=ID).headers["set-cookie"].startswith("mtk_session=tailnet.")
    assert by_tailscale.post("/api/session/remember").status_code == 403


def test_a_server_that_does_not_keep_devices_signed_in_hands_out_and_takes_no_key(tmp_path: Path) -> None:
    web = _web(tmp_path, login_days=0)  # (the desktop app)
    device = _signed_in(web)
    gone = {"detail": "this server does not keep devices signed in"}
    assert device.post("/api/session/remember").status_code == 404 and device.post("/api/session/remember").json() == gone
    assert device.get("/api/session").json()["remember_days"] == 0
    refused = _device(web).post("/signin/restore", data={"key": _key()})
    assert refused.status_code == 404 and refused.json() == gone and "set-cookie" not in refused.headers
    open_app = build_web(tmp_path / "open", None, token=None, login_days=90)  # (and an app that has no token has nothing to sign in to)
    assert open_app.client.post("/api/session/remember").status_code == 404
    assert open_app.client.post("/signin/restore", data={"key": _key()}).status_code == 404
    assert open_app.client.get("/api/session").json()["remember_days"] == 0


def test_a_server_that_keeps_devices_signed_in_for_one_day_does_hand_out_and_take_keys(tmp_path: Path) -> None:
    web = _web(tmp_path, login_days=1)
    given = _signed_in(web).post("/api/session/remember")
    assert given.status_code == 200 and given.json()["days"] == 1 and abs(given.json()["expires"] - (time.time() + DAY)) < 5
    restored = _device(web).post("/signin/restore", data={"key": given.json()["key"]})
    assert restored.status_code == 204 and "max-age=86400" in restored.headers["set-cookie"].lower()


@pytest.mark.filterwarnings("ignore:Duplicate Operation ID")  # (FastAPI says it of routes that were there before: not what this test is about)
def test_the_schema_that_is_served_to_everyone_does_not_list_the_sign_in_routes(tmp_path: Path) -> None:
    served = _device(_web(tmp_path)).get("/openapi.json").json()["paths"]
    hidden = {"/", "/signin", "/signin/restore", "/api/session", "/api/session/remember", "/api/session/signout", "/sw.js", "/healthz"}
    assert hidden & set(served) == set()


def test_the_session_says_for_how_many_days_a_device_is_kept_signed_in(tmp_path: Path) -> None:
    assert _signed_in(_web(tmp_path, login_days=30)).get("/api/session").json()["remember_days"] == 30


# ---------------------------------------------------------------------------------------------------- taking the key


def test_the_right_key_gets_a_device_the_cookie_the_token_buys(tmp_path: Path) -> None:
    web = _web(tmp_path)
    device = _device(web)
    assert device.get("/api/about").status_code == 401
    answer = device.post("/signin/restore", data={"key": _key()})
    assert answer.status_code == 204 and answer.content == b""
    cookie = answer.headers["set-cookie"].lower()
    assert cookie.startswith(f"mtk_session={TOKEN.lower()};") and "httponly" in cookie and "samesite=lax" in cookie and "max-age=7776000" in cookie and "path=/" in cookie
    assert "; secure" not in cookie
    assert device.get("/api/about").status_code == 200  # (the cookie rides along)
    secure = _device(web).post("https://testserver/signin/restore", data={"key": _key()}).headers["set-cookie"].lower()
    assert "; secure" in secure  # (over https, as every cookie the app gives)


def test_a_key_that_is_wrong_ended_or_made_from_another_token_gets_nothing(tmp_path: Path) -> None:
    web = _web(tmp_path)
    for key in ("", "nope", _key()[:-1] + "!", remember.make(TOKEN, int(time.time()) - 5), remember.make(TOKEN + "-changed", int(time.time()) + DAY), TOKEN):
        device = _device(web)
        answer = device.post("/signin/restore", data={"key": key})
        assert answer.status_code == 401 and answer.json() == {"detail": "that key is not good"} and "set-cookie" not in answer.headers
        assert device.get("/api/about").status_code == 401
        web.clock.advance(61)  # (the limit on wrong ones is another test's)
    assert _device(web).post("/signin/restore").status_code == 401  # (no key at all)


def test_a_form_without_a_key_is_a_wrong_key(tmp_path: Path) -> None:
    web = _web(tmp_path)
    answer = _device(web).post("/signin/restore", data={})
    assert answer.status_code == 401 and "set-cookie" not in answer.headers


def test_wrong_keys_are_counted_like_wrong_tokens_and_while_too_many_are_wrong_no_key_is_looked_at(tmp_path: Path) -> None:
    web = _web(tmp_path)
    for number in range(4):
        assert _device(web).post("/signin", data={"token": f"wrong-{number}"}).status_code == 401
        assert _device(web).post("/signin/restore", data={"key": f"wrong-{number}"}).status_code == 401
    answer = _device(web).post("/signin/restore", data={"key": _key()})  # (eight wrong, of both kinds: the right key is not looked at either)
    assert answer.status_code == 429 and answer.headers["retry-after"] == "60" and "Try again in 60 seconds." in answer.json()["detail"] and "set-cookie" not in answer.headers
    assert _device(web).post("/signin", data={"token": TOKEN}).status_code == 429  # (nor is the token)
    web.clock.advance(61)
    assert _device(web).post("/signin/restore", data={"key": _key()}).status_code == 204


def test_a_device_that_is_already_signed_in_is_told_so_and_a_wrong_key_from_it_is_not_counted(tmp_path: Path) -> None:
    web = _web(tmp_path)
    device = _signed_in(web)
    for _ in range(20):
        answer = device.post("/signin/restore", data={"key": "wrong"})
        assert answer.status_code == 204 and "set-cookie" not in answer.headers
    assert _device(web).post("/signin/restore", data={"key": "wrong"}).status_code == 401  # (not 429: none of the twenty was counted)


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_page_of_another_site_cannot_sign_a_browser_in_with_a_key_and_the_try_is_not_counted(tmp_path: Path, site: str) -> None:
    web = _web(tmp_path)
    for _ in range(12):
        answer = _device(web).post("/signin/restore", data={"key": "guess"}, headers={"sec-fetch-site": site})
        assert answer.status_code == 403 and answer.json() == {"detail": "a sign-in has to be made from this app's own page"} and "set-cookie" not in answer.headers
    right = _device(web).post("/signin/restore", data={"key": _key()}, headers={"sec-fetch-site": site})
    assert right.status_code == 403 and "set-cookie" not in right.headers
    for own in ("same-origin", "none"):  # (the page's own request, and a browser that says nothing, are let in)
        assert _device(web).post("/signin/restore", data={"key": _key()}, headers={"sec-fetch-site": own}).status_code == 204
    assert _device(web).post("/signin/restore", data={"key": _key()}).status_code == 204


def test_the_address_of_the_key_is_for_posting_to_only(tmp_path: Path) -> None:
    web = _web(tmp_path)
    assert _device(web).get("/signin/restore").status_code == 405
    assert _device(web).get("/signin/restore", params={"key": _key()}).status_code == 405  # (a key in an address would be written down in places)


# ---------------------------------------------------------------------------------------------------- signing out


def test_signing_out_takes_the_cookie_away(tmp_path: Path) -> None:
    web = _web(tmp_path)
    device = _signed_in(web)
    answer = device.post("/api/session/signout")
    assert answer.status_code == 204
    cookie = answer.headers["set-cookie"].lower()
    assert cookie.startswith('mtk_session="";') and "max-age=0" in cookie and "path=/" in cookie
    assert device.get("/api/about").status_code == 401
    assert _device(web).post("/api/session/signout").status_code == 401  # (a device that is not signed in has nothing to sign out of)


# ---------------------------------------------------------------------------------------------------- a request from another site


REFUSED = {"detail": "this request came from a page of another site"}


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_link_followed_from_another_site_and_a_request_that_changes_something_are_refused_though_the_cookie_rides_along(tmp_path: Path, site: str) -> None:
    web = _web(tmp_path)
    device = _signed_in(web)
    link = {"sec-fetch-site": site, "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    answers = [device.get("/api/about", headers=link), device.head("/api/about", headers=link), device.get("/api/session", headers=link)]
    answers += [device.request(method, "/api/session/signout", headers={"sec-fetch-site": site, "sec-fetch-mode": "cors"}) for method in ("POST", "PUT", "PATCH", "DELETE")]
    for answer in answers:
        assert answer.status_code == 403 and "set-cookie" not in answer.headers  # (nothing is taken away from the device: a link on another site must not sign it out)
        assert answer.request.method == "HEAD" or answer.json() == REFUSED
    assert device.get("/api/about").status_code == 200  # (the device is as signed in as it was)
    assert device.get("/api/about", headers={**link, "x-mtk-token": TOKEN}).status_code == 403  # (whatever else it carries)


@pytest.mark.parametrize(
    ("site", "method", "mode", "refused"),
    [
        ("cross-site", "GET", "navigate", True), ("same-site", "GET", "navigate", True), ("cross-site", "HEAD", "navigate", True), ("same-site", "HEAD", "navigate", True),
        ("cross-site", "POST", "cors", True), ("same-site", "POST", "no-cors", True), ("cross-site", "POST", "", True), ("same-site", "POST", "navigate", True),
        ("cross-site", "PUT", "cors", True), ("cross-site", "PATCH", "cors", True), ("same-site", "DELETE", "cors", True),
        # a page of another site that only reads (a song, a cover, a call that changes nothing): it cannot read the answer, and music must not stop for a browser's mistake
        ("cross-site", "GET", "cors", False), ("cross-site", "GET", "no-cors", False), ("same-site", "GET", "same-origin", False), ("cross-site", "GET", "", False),
        ("same-site", "HEAD", "no-cors", False),
        # the app's own requests, an address typed or an icon, and a program (which says nothing)
        ("same-origin", "GET", "navigate", False), ("none", "GET", "navigate", False), ("same-origin", "POST", "cors", False), ("none", "POST", "", False),
        ("", "POST", "", False), ("", "GET", "navigate", False), ("", "GET", "", False),
    ],
)
def test_what_a_browser_says_of_where_a_request_came_from_decides_whether_it_is_answered(tmp_path: Path, site: str, method: str, mode: str, refused: bool) -> None:
    device = _signed_in(_web(tmp_path))
    said = {name: value for name, value in {"sec-fetch-site": site, "sec-fetch-mode": mode}.items() if value}
    answer = device.request(method, "/api/about", headers=said)
    assert (answer.status_code == 403) is refused, answer.status_code
    if refused and method != "HEAD":
        assert answer.json() == REFUSED


def test_a_request_without_a_cookie_from_another_site_is_refused_as_before(tmp_path: Path) -> None:
    web = _web(tmp_path)
    answer = _device(web).get("/api/about", headers={"sec-fetch-site": "cross-site"})
    assert answer.status_code == 401 and answer.json() == {"detail": "unauthorized"}


def test_the_page_itself_is_opened_from_a_link_on_another_site_signed_in(tmp_path: Path) -> None:
    """What Strict could not do, and Lax is for: a link in a message or on a web page, or the app's icon, opens the app with the device signed in."""
    device = _signed_in(_web(tmp_path))
    for site in ("cross-site", "same-site", "none"):
        answer = device.get("/", headers={"sec-fetch-site": site, "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"})
        assert answer.status_code == 200 and "Access token" not in answer.text and "set-cookie" not in answer.headers
    assert device.get("/static/js/main.js", headers={"sec-fetch-site": "cross-site"}).status_code == 200


# ---------------------------------------------------------------------------------------------------- the page says why it asks


def test_the_page_that_asks_for_the_token_says_what_the_request_carried(tmp_path: Path) -> None:
    page = _device(_web(tmp_path)).get("/", headers={**DOCUMENT, "service-worker-navigation-preload": "true"})
    assert page.status_code == 401 and "<details" in page.text and "Why does it ask for the token?" in page.text
    assert "No sign-in cookie came with this request." in page.text and "opened by hand: typed, from a bookmark, or from the app&#x27;s icon" in page.text
    assert "<code>cookie=none site=none mode=navigate dest=document preload=yes</code>" in page.text
    assert TOKEN not in page.text and "retry-after" not in page.headers  # (it is not asked to wait: only the page that says to wait is)


def test_the_page_says_so_when_the_cookie_that_came_is_not_taken(tmp_path: Path) -> None:
    device = _device(_web(tmp_path))
    page = device.get("/", headers={**DOCUMENT, "cookie": "mtk_session=the-old-token"})
    assert page.status_code == 401 and "A sign-in cookie came with this request, but the server does not take it" in page.text
    assert "<code>cookie=refused site=none mode=navigate dest=document preload=no</code>" in page.text and "the-old-token" not in page.text


@pytest.mark.parametrize(
    ("headers", "line", "words"),
    [
        ({}, "cookie=none site=unsaid mode=unsaid dest=unsaid preload=no", "The browser did not say how the page was opened."),
        ({"sec-fetch-site": "cross-site"}, "cookie=none site=cross-site mode=unsaid dest=unsaid preload=no", "The page was opened from a page of another site."),
        ({"sec-fetch-site": "same-site"}, "cookie=none site=same-site mode=unsaid dest=unsaid preload=no", "The page was opened from another page of the same site."),
        ({"sec-fetch-site": "same-origin"}, "cookie=none site=same-origin mode=unsaid dest=unsaid preload=no", "The page was opened from a page of this app (a reload, or a link inside it)."),
        ({"sec-fetch-site": "NONE"}, "cookie=none site=none mode=unsaid dest=unsaid preload=no", "The page was opened by hand: typed, from a bookmark, or from the app's icon."),
        ({"sec-fetch-site": "<b>x</b>", "sec-fetch-mode": "a" * 40, "sec-fetch-dest": "x y"}, "cookie=none site=other mode=other dest=other preload=no", "The browser said something unusual about how the page was opened."),
    ],
)
def test_what_a_browser_says_in_its_headers_is_repeated_as_words_and_nothing_else(tmp_path: Path, headers: dict, line: str, words: str) -> None:
    page = _device(_web(tmp_path)).get("/", headers=headers)
    assert f"<code>{line}</code>" in page.text and f"<p>{html.escape(words)}</p>" in page.text
    assert "<b>x</b>" not in page.text and "a" * 40 not in page.text


def test_the_page_does_not_explain_itself_after_a_wrong_token_or_while_it_asks_to_wait(tmp_path: Path) -> None:
    web = _web(tmp_path)
    wrong = _device(web).post("/signin", data={"token": "nope"})
    assert wrong.status_code == 401 and "That is not the token." in wrong.text and "<details" not in wrong.text
    link = _device(web).get("/?token=nope")
    assert link.status_code == 401 and "<details" not in link.text
    for number in range(8):
        _device(web).post("/signin", data={"token": f"wrong-{number}"})
    paused = _device(web).get("/?token=whatever")
    assert paused.status_code == 429 and "<details" not in paused.text


def test_the_first_visit_is_written_in_the_log_once_with_nothing_of_the_request_but_those_words(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    web = _web(tmp_path)
    caplog.set_level(logging.INFO, logger="musictoolkit")
    device = _device(web)
    device.get("/", headers={**DOCUMENT, "cookie": "mtk_session=the-old-token", "user-agent": "Secret-Agent/1.0"})
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Sign-in page shown")]
    assert said == ["Sign-in page shown: cookie=refused site=none mode=navigate dest=document preload=no"]
    device.head("/")  # (nothing is learned from a request that asks only for headers)
    device.post("/signin", data={"token": "wrong-token-text"})
    device.get("/?token=another-wrong-token")
    assert len([r for r in caplog.records if r.getMessage().startswith("Sign-in page shown")]) == 1
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "the-old-token" not in text and "Secret-Agent" not in text and "wrong-token-text" not in text and "another-wrong-token" not in text and TOKEN not in text


def test_the_page_tries_the_saved_key_where_devices_are_kept_signed_in_and_only_there(tmp_path: Path) -> None:
    kept = _device(_web(tmp_path / "kept")).get("/")
    assert '<script src="/static/js/restore.js" defer></script>' in kept.text and 'id="restore-note"' in kept.text
    not_kept = _device(_web(tmp_path / "not", login_days=0)).get("/")
    assert "restore.js" not in not_kept.text and "restore-note" not in not_kept.text
    assert "restore.js" in sign_in_page(login_days=1) and "restore.js" not in sign_in_page()
    assert _device(_web(tmp_path / "other")).get("/static/js/restore.js").status_code == 200  # (the page's script is for a device that is not signed in)


def test_the_page_says_for_how_many_days_a_device_stays_signed_in_and_says_so_right_for_one() -> None:
    assert "The device then stays signed in for 1 day." in sign_in_page(login_days=1)
    assert "The device then stays signed in for 2 days." in sign_in_page(login_days=2)
    assert "stays signed in" not in sign_in_page(login_days=0)


def test_a_page_that_is_told_whom_tailscale_names_and_nothing_else_offers_the_line_that_would_sign_that_account_in() -> None:
    page = sign_in_page(tailscale_login=YOU)  # (the server signs in no account, and this one is not among them)
    assert "This server can sign this device in through Tailscale, with no token." in page and html.escape('tailscale_logins = ["you@example.com"]') in page
    assert "does not sign in that account" not in page and "opened from another site" not in page
    listed = sign_in_page(tailscale_login=YOU, tailscale_on=True)  # (the server signs in some accounts, and this one is not among them)
    assert "This server does not sign in that account." in listed and "opened from another site" not in listed


def test_the_words_of_the_request_are_made_by_the_arrival_function_from_the_headers_it_is_given() -> None:
    got = arrival({"sec-fetch-site": "Cross-Site", "sec-fetch-mode": "navigate", "service-worker-navigation-preload": "true"}, "x")
    assert (got.cookie, got.site, got.mode, got.dest, got.preload) == ("refused", "cross-site", "navigate", "", True)
    assert got.line() == "cookie=refused site=cross-site mode=navigate dest=unsaid preload=yes"
    assert arrival({}, None).cookie == "none" and arrival({}, "").cookie == "none"
    with pytest.raises(dataclasses.FrozenInstanceError):  # (what was seen is not changed afterwards)
        got.cookie = "none"  # type: ignore[misc]
    assert signin.why_note(arrival({}, None)).count("<details") == 1
