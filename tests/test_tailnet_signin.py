"""Signing in with the Tailscale identity, through the whole app: the page, the API, the cookie, what is refused, and a real server behind Serve's headers."""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from musictoolkit.cli import app as cli_app
from musictoolkit.web import tailnet
from tests.conftest import build_web

SRC = str(Path(__file__).resolve().parent.parent / "src")
TOKEN = "k" * 8 + "T0ken" + "z" * 30
YOU = "you@example.com"
ID = {"tailscale-user-login": YOU}  # (what `tailscale serve` adds to a request from the person's own device)
BROWSER = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"


def _web(tmp_path: Path, logins: tuple[str, ...] | list[str] = (YOU,), token: str | None = TOKEN, **options):
    return build_web(tmp_path, None, token=token, tailscale_logins=list(logins), login_days=90, **options)


def _device(web) -> TestClient:
    """Another device on the same app: its own cookies."""
    return TestClient(web.app, raise_server_exceptions=False)


def _stale(response) -> bool:
    """Does the response tell the browser to drop its session cookie?"""
    cookie = response.headers.get("set-cookie", "").lower()
    return cookie.startswith('mtk_session="";') and "max-age=0" in cookie


# ---------------------------------------------------------------------------------------------------- signing in


def test_a_listed_account_is_signed_in_by_opening_the_app_and_the_cookie_it_gets_does_the_rest(tmp_path: Path) -> None:
    web = _web(tmp_path)
    device = _device(web)
    page = device.get("/", headers=ID)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html") and "<title>" in page.text
    cookie = page.headers["set-cookie"]
    assert cookie.startswith(f"mtk_session={tailnet.session_cookie(TOKEN, YOU)};")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "max-age=7776000" in lowered and "path=/" in lowered and "; secure" not in lowered
    assert TOKEN not in cookie and YOU not in cookie
    assert device.get("/api/about").status_code == 200  # (the cookie alone: no header this time)
    assert "set-cookie" not in device.get("/api/about").headers  # (and nothing is handed out again)


def test_a_listed_account_is_signed_in_by_the_first_call_to_the_api_too(tmp_path: Path) -> None:
    web = _web(tmp_path)
    device = _device(web)
    first = device.get("/api/about", headers=ID)
    assert first.status_code == 200 and first.headers["set-cookie"].startswith(f"mtk_session={tailnet.session_cookie(TOKEN, YOU)};")
    assert device.get("/api/about").status_code == 200
    assert _device(web).get("/api/about").status_code == 401  # (another device has nothing: the cookie was this one's)


def test_the_cookie_is_secure_over_https_the_way_the_tokens_is(tmp_path: Path) -> None:
    web = _web(tmp_path)
    plain = _device(web).get("http://testserver/", headers=ID).headers["set-cookie"].lower()
    secure = _device(web).get("https://testserver/", headers=ID).headers["set-cookie"].lower()
    assert "; secure" not in plain and "; secure" in secure


def test_without_login_days_the_cookie_ends_with_the_window(tmp_path: Path) -> None:
    web = build_web(tmp_path, None, token=TOKEN, tailscale_logins=[YOU])
    cookie = _device(web).get("/", headers=ID).headers["set-cookie"].lower()
    assert "max-age" not in cookie and "expires" not in cookie


def test_logins_are_matched_without_regard_to_capitals_and_the_listed_ones_may_be_written_with_them(tmp_path: Path) -> None:
    web = _web(tmp_path, logins=["You@Example.COM"])
    assert _device(web).get("/api/about", headers={"tailscale-user-login": "YOU@example.com"}).status_code == 200
    assert _device(web).get("/api/about", headers={"tailscale-user-login": "you@example.com"}).status_code == 200


def test_a_login_with_letters_that_are_not_ascii_arrives_encoded_and_is_matched(tmp_path: Path) -> None:
    web = _web(tmp_path, logins=["jürgen@example.com"])
    assert _device(web).get("/api/about", headers={"tailscale-user-login": "=?utf-8?q?J=C3=BCrgen@example.com?="}).status_code == 200
    assert _device(web).get("/api/about", headers={"tailscale-user-login": "=?utf-8?q?Juergen@example.com?="}).status_code == 401


def test_each_of_several_listed_accounts_is_signed_in_with_a_cookie_of_its_own(tmp_path: Path) -> None:
    web = _web(tmp_path, logins=[YOU, "partner@example.com"])
    mine = _device(web).get("/api/about", headers=ID).cookies["mtk_session"]
    theirs = _device(web).get("/api/about", headers={"tailscale-user-login": "partner@example.com"}).cookies["mtk_session"]
    assert mine != theirs and mine == tailnet.session_cookie(TOKEN, YOU) and theirs == tailnet.session_cookie(TOKEN, "partner@example.com")
    assert _device(web).get("/api/about", headers={"cookie": f"mtk_session={mine}"}).status_code == 200
    assert _device(web).get("/api/about", headers={"cookie": f"mtk_session={theirs}"}).status_code == 200


def test_a_request_from_a_program_is_signed_in_every_time_and_a_head_request_is_too(tmp_path: Path) -> None:
    web = _web(tmp_path)
    program = {**ID, "user-agent": "Amperfy/1.0 CFNetwork/1494.0.7 Darwin/23.4.0"}
    for _ in range(3):
        assert _device(web).get("/api/about", headers=program).status_code == 200
    head = _device(web).head("/", headers=ID)
    assert head.status_code == 200 and head.headers["set-cookie"].startswith("mtk_session=tailnet.")


def test_the_token_works_as_before_and_gives_the_token_as_the_cookie(tmp_path: Path) -> None:
    web = _web(tmp_path)
    device = _device(web)
    page = device.get(f"/?token={TOKEN}")
    assert page.status_code == 200 and page.headers["set-cookie"].startswith(f"mtk_session={TOKEN};")
    assert device.get("/api/about", headers=ID).status_code == 200 and "set-cookie" not in device.get("/api/about", headers=ID).headers  # (already in: the cookie is not swapped)
    assert _device(web).get("/api/about", headers={"x-mtk-token": TOKEN}).status_code == 200
    assert _device(web).post("/signin", data={"token": TOKEN}, follow_redirects=False).headers["set-cookie"].startswith(f"mtk_session={TOKEN};")


def test_the_token_form_is_not_used_by_a_device_tailscale_vouches_for_and_nothing_is_counted(tmp_path: Path) -> None:
    web = _web(tmp_path)
    for _ in range(20):
        answer = _device(web).post("/signin", data={"token": "wrong"}, headers=ID, follow_redirects=False)
        assert answer.status_code == 303 and answer.headers["location"] == "/" and answer.headers["set-cookie"].startswith("mtk_session=tailnet.")
    assert web.app.state.limiter.wait() == 0


def test_a_link_with_a_wrong_token_does_not_stop_a_device_tailscale_vouches_for(tmp_path: Path) -> None:
    web = _web(tmp_path)
    page = _device(web).get("/?token=wrong", headers=ID)
    assert page.status_code == 200 and page.headers["set-cookie"].startswith("mtk_session=tailnet.")
    assert web.app.state.limiter.wait() == 0


def test_what_tailscale_vouches_for_is_never_counted_as_a_wrong_token_and_is_let_in_while_wrong_ones_are_locked_out(tmp_path: Path) -> None:
    web = _web(tmp_path)
    for _ in range(20):
        assert _device(web).get("/api/about", headers={**ID, "x-mtk-token": "wrong"}).status_code == 200
    assert web.app.state.limiter.wait() == 0
    stranger = {"tailscale-user-login": "mallory@example.com", "x-mtk-token": "wrong"}
    for _ in range(8):
        assert _device(web).get("/api/about", headers=stranger).status_code == 401
    assert _device(web).get("/api/about", headers=stranger).status_code == 429  # (guessing is still slowed)
    assert _device(web).get("/api/about", headers=ID).status_code == 200  # (a device Tailscale vouches for is not locked out with the guessers)
    assert _device(web).get("/", headers=ID).status_code == 200


# ---------------------------------------------------------------------------------------------------- who is not signed in


@pytest.mark.parametrize("path", ["/", "/api/about", "/api/tracks", "/api/session"])
def test_an_account_that_is_not_listed_is_not_signed_in_and_nothing_is_given(tmp_path: Path, path: str) -> None:
    web = _web(tmp_path)
    stranger = {"tailscale-user-login": "mallory@example.com"}
    answer = _device(web).get(path, headers=stranger)
    assert answer.status_code == 401 and "set-cookie" not in answer.headers
    if path == "/":
        assert "<strong>mallory@example.com</strong>" in answer.text and "This server does not sign in that account." in answer.text
        assert YOU not in answer.text  # (who is let in is not told to someone who is not)
    else:
        assert answer.json() == {"detail": "unauthorized"} and "mallory" not in answer.text


@pytest.mark.parametrize("path", ["/", "/api/about", "/api/tracks", "/api/session"])
def test_a_server_that_signs_nobody_in_does_not_believe_the_header_and_offers_the_line_that_would(tmp_path: Path, path: str) -> None:
    web = _web(tmp_path, logins=[])
    answer = _device(web).get(path, headers=ID)  # (anyone can write that header at a server that is not behind Serve: it must do nothing)
    assert answer.status_code == 401 and "set-cookie" not in answer.headers
    if path == "/":
        assert "<pre>tailscale_logins = [&quot;you@example.com&quot;]</pre>" in answer.text


def test_a_page_without_the_header_says_nothing_about_tailscale(tmp_path: Path) -> None:
    for logins in ([], [YOU]):
        page = _device(_web(tmp_path / str(len(logins)), logins=logins)).get("/")
        assert page.status_code == 401 and "Tailscale" not in page.text and "tailscale_logins" not in page.text


def test_a_login_in_the_page_is_text_and_not_markup(tmp_path: Path) -> None:
    web = _web(tmp_path, logins=[])
    page = _device(web).get("/", headers={"tailscale-user-login": "<b>x</b>@example.com"})
    assert "&lt;b&gt;x&lt;/b&gt;@example.com" in page.text and "<b>x</b>" not in page.text


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
@pytest.mark.parametrize("path", ["/", "/api/about", "/api/tracks"])
def test_a_page_of_another_site_cannot_use_the_identity_of_the_person_whose_browser_it_runs_in(tmp_path: Path, site: str, path: str) -> None:
    web = _web(tmp_path)
    answer = _device(web).get(path, headers={**ID, "sec-fetch-site": site, "user-agent": BROWSER})
    assert answer.status_code == 401 and "set-cookie" not in answer.headers
    if path == "/":
        assert "this page was opened from another site" in answer.text and "Open the server's address itself" in answer.text


@pytest.mark.parametrize("method,path,status", [("POST", "/api/library/scan", 401), ("DELETE", "/api/playlists/1", 401), ("POST", "/signin", 403)])
def test_nor_can_it_do_anything_that_changes_something(tmp_path: Path, method: str, path: str, status: int) -> None:
    web = _web(tmp_path)
    answer = _device(web).request(method, path, headers={**ID, "sec-fetch-site": "cross-site", "user-agent": BROWSER})
    assert answer.status_code == status and "set-cookie" not in answer.headers


def test_a_playlist_a_page_of_another_site_tried_to_make_through_the_visitors_identity_is_not_made(tmp_path: Path) -> None:
    web = _web(tmp_path)
    attack = _device(web).post("/api/playlists", json={"name": "made by another site"}, headers={**ID, "sec-fetch-site": "cross-site", "user-agent": BROWSER})
    assert attack.status_code == 401
    owner = _device(web)
    assert owner.get("/api/about", headers=ID).status_code == 200  # (the person's own page, a moment later)
    assert owner.get("/api/playlists").json() == {"items": []}  # (nothing was made)
    assert owner.post("/api/playlists", json={"name": "made by the person"}).status_code == 201
    assert [item["name"] for item in owner.get("/api/playlists").json()["items"]] == ["made by the person"]


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_the_apps_own_page_and_the_address_bar_are_signed_in(tmp_path: Path, site: str) -> None:
    web = _web(tmp_path)
    for path in ("/", "/api/about"):
        assert _device(web).get(path, headers={**ID, "sec-fetch-site": site, "user-agent": BROWSER}).status_code == 200


def test_a_browser_too_old_to_say_where_a_request_came_from_is_not_signed_in_and_a_program_is(tmp_path: Path) -> None:
    web = _web(tmp_path)
    assert _device(web).get("/api/about", headers={**ID, "user-agent": BROWSER}).status_code == 401
    assert _device(web).get("/api/about", headers={**ID, "user-agent": "curl/8.5.0"}).status_code == 200
    assert _device(web).get("/api/about", headers=ID).status_code == 200  # (the test client's own name is not a browser's)


def test_two_logins_in_one_request_are_nobodys(tmp_path: Path) -> None:
    web = _web(tmp_path, logins=[YOU, "x@example.com"])
    both = [("tailscale-user-login", YOU), ("tailscale-user-login", "x@example.com")]
    assert _device(web).get("/api/about", headers=both).status_code == 401
    assert _device(web).get("/api/about", headers=[("tailscale-user-login", YOU)]).status_code == 200


@pytest.mark.parametrize("name", ["tailscale_user_login", "x-tailscale-user-login", "tailscale-user-name", "tailscale-user-profile-pic"])
def test_only_the_one_header_tailscale_writes_for_the_login_is_read(tmp_path: Path, name: str) -> None:
    web = _web(tmp_path)
    assert _device(web).get("/api/about", headers={name: YOU}).status_code == 401


def test_the_host_is_checked_before_anything_else_so_a_rebound_name_gets_nothing_even_from_a_listed_account(tmp_path: Path) -> None:
    web = _web(tmp_path)
    answer = _device(web).get("/api/about", headers={**ID, "host": "evil.example"})
    assert answer.status_code == 403 and answer.json() == {"detail": "forbidden host"} and "set-cookie" not in answer.headers
    named = _web(tmp_path / "named", allowed_hosts=["musicpi.tail1234.ts.net"])
    assert _device(named).get("/api/about", headers={**ID, "host": "musicpi.tail1234.ts.net"}).status_code == 200


def test_an_app_without_a_token_hands_out_no_cookie_whoever_is_named(tmp_path: Path) -> None:
    web = _web(tmp_path, token=None)
    for path in ("/", "/api/about"):
        answer = _device(web).get(path, headers=ID)
        assert answer.status_code == 200 and "set-cookie" not in answer.headers


# ---------------------------------------------------------------------------------------------------- Funnel


@pytest.mark.parametrize("path", ["/", "/api/about", "/healthz", "/sw.js", "/static/js/main.js", "/api/session", "/signin"])
@pytest.mark.parametrize("marked", [{"tailscale-funnel-request": "?1"}, {"tailscale-funnel-request": ""}])
def test_a_request_that_came_through_funnel_is_refused_whatever_it_asks_for(tmp_path: Path, path: str, marked: dict[str, str]) -> None:
    web = _web(tmp_path)
    answer = _device(web).get(path, headers=marked)
    assert answer.status_code == 403 and answer.json() == {"detail": "this server is not available through Tailscale Funnel"}
    assert "set-cookie" not in answer.headers and "content-security-policy" not in answer.headers
    signed = _device(web)
    signed.get(f"/?token={TOKEN}")
    assert signed.get(path, headers=marked).status_code == 403  # (not even a device that has the token)
    assert signed.get(path).status_code in (200, 303)  # (the same device is let in when it did not come through Funnel)


def test_funnel_is_refused_by_a_server_that_signs_nobody_in_and_one_without_a_token_too(tmp_path: Path) -> None:
    assert _device(_web(tmp_path, logins=[])).get("/healthz", headers={"tailscale-funnel-request": "?1"}).status_code == 403
    assert _device(_web(tmp_path / "open", token=None)).get("/api/about", headers={"tailscale-funnel-request": "?1"}).status_code == 403


def test_a_post_through_funnel_is_refused_before_a_wrong_token_is_counted(tmp_path: Path) -> None:
    web = _web(tmp_path)
    for _ in range(12):
        assert _device(web).post("/signin", data={"token": "wrong"}, headers={"tailscale-funnel-request": "?1"}).status_code == 403
    assert web.app.state.limiter.wait() == 0  # (the refusal is at the door: nobody can use Funnel to lock the owner out of guessing, or to guess)


# ---------------------------------------------------------------------------------------------------- taking someone off the list, and changing the token


def test_taking_an_account_off_the_list_signs_its_devices_out_on_their_next_request(tmp_path: Path) -> None:
    cookie = _device(_web(tmp_path / "before", logins=[YOU, "partner@example.com"])).get("/", headers=ID).cookies["mtk_session"]
    asked = {"cookie": f"mtk_session={cookie}"}
    assert _device(_web(tmp_path / "same", logins=[YOU, "other@example.com"])).get("/api/about", headers=asked).status_code == 200
    gone = _device(_web(tmp_path / "after", logins=["partner@example.com"])).get("/api/about", headers=asked)
    assert gone.status_code == 401 and _stale(gone)  # (and the browser is told to drop it)
    page = _device(_web(tmp_path / "page", logins=["partner@example.com"])).get("/", headers=asked)
    assert page.status_code == 401


def test_the_cookie_of_one_account_does_not_sign_in_another_that_is_listed_instead(tmp_path: Path) -> None:
    partner = _device(_web(tmp_path / "a", logins=["partner@example.com"])).get("/api/about", headers={"tailscale-user-login": "partner@example.com"}).cookies["mtk_session"]
    assert _device(_web(tmp_path / "b", logins=[YOU])).get("/api/about", headers={"cookie": f"mtk_session={partner}"}).status_code == 401


def test_changing_the_token_signs_every_device_out_but_the_ones_tailscale_vouches_for_sign_in_again(tmp_path: Path) -> None:
    cookie = _device(_web(tmp_path / "old")).get("/", headers=ID).cookies["mtk_session"]
    rotated = _web(tmp_path / "new", token=TOKEN + "-rotated")
    device = _device(rotated)
    assert device.get("/api/about", headers={"cookie": f"mtk_session={cookie}"}).status_code == 401
    again = device.get("/api/about", headers={**ID, "cookie": f"mtk_session={cookie}"})  # (the stale cookie and the header together)
    assert again.status_code == 200 and again.headers["set-cookie"].startswith(f"mtk_session={tailnet.session_cookie(TOKEN + '-rotated', YOU)};")
    assert tailnet.session_cookie(TOKEN + "-rotated", YOU) != cookie


def test_a_cookie_of_this_kind_is_nothing_to_a_server_that_signs_nobody_in(tmp_path: Path) -> None:
    cookie = _device(_web(tmp_path / "on")).get("/", headers=ID).cookies["mtk_session"]
    off = _device(_web(tmp_path / "off", logins=[])).get("/api/about", headers={"cookie": f"mtk_session={cookie}"})
    assert off.status_code == 401 and _stale(off)


def test_the_cookie_a_login_gets_is_not_the_token_so_it_does_not_sign_in_by_the_other_way(tmp_path: Path) -> None:
    web = _web(tmp_path)
    cookie = _device(web).get("/", headers=ID).cookies["mtk_session"]
    assert _device(web).get("/api/about", headers={"x-mtk-token": cookie}).status_code == 401  # (it is not a token)
    assert _device(web).get(f"/?token={cookie}").status_code == 401


# ---------------------------------------------------------------------------------------------------- Settings asks how it is signed in


def test_the_app_can_say_how_a_device_is_signed_in_and_what_tailscale_says_about_it(tmp_path: Path) -> None:
    web = _web(tmp_path)
    by_token = _device(web)
    by_token.get(f"/?token={TOKEN}")
    assert by_token.get("/api/session").json() == {"by": "token", "remember_days": 90, "tailscale": {"on": True, "login": None, "allowed": False, "own_page": True}}
    both = by_token.get("/api/session", headers=ID).json()  # (the cookie got it in: that is how it is said to be signed in, though Tailscale would too)
    assert both["by"] == "token" and both["tailscale"] == {"on": True, "login": YOU, "allowed": True, "own_page": True}
    assert by_token.get("/api/session", headers={"tailscale-user-login": "mallory@example.com"}).json()["tailscale"] == {
        "on": True, "login": "mallory@example.com", "allowed": False, "own_page": True,
    }
    assert by_token.get("/api/session", headers={**ID, "sec-fetch-site": "cross-site"}).json()["tailscale"]["own_page"] is False  # (a page of another site that only reads: not asked)
    link = by_token.get("/api/session", headers={**ID, "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"})  # (the cookie is Lax: it rides along on a link followed from another site)
    assert link.status_code == 403 and link.json() == {"detail": "this request came from a page of another site"}

    by_tailscale = _device(web)
    first = by_tailscale.get("/api/session", headers=ID)
    assert first.json()["by"] == "tailscale" and first.headers["set-cookie"].startswith("mtk_session=tailnet.")
    assert by_tailscale.get("/api/session").json() == {"by": "tailscale", "remember_days": 90, "tailscale": {"on": True, "login": None, "allowed": False, "own_page": True}}

    assert _device(web).get("/api/session").status_code == 401  # (nobody who is not signed in is told anything)


def test_a_server_that_signs_nobody_in_says_so_and_an_app_without_a_token_says_it_is_open(tmp_path: Path) -> None:
    off = _device(_web(tmp_path / "off", logins=[]))
    off.get(f"/?token={TOKEN}")
    assert off.get("/api/session", headers=ID).json() == {"by": "token", "remember_days": 90, "tailscale": {"on": False, "login": YOU, "allowed": False, "own_page": True}}
    open_app = _device(_web(tmp_path / "open", token=None, logins=[]))
    assert open_app.get("/api/session").json() == {"by": "open", "remember_days": 0, "tailscale": {"on": False, "login": None, "allowed": False, "own_page": True}}


def test_an_app_that_is_given_a_login_that_is_not_one_is_not_made(tmp_path: Path) -> None:
    for number, bad in enumerate((["you smith@example.com"], [""], [5])):
        with pytest.raises(ValueError):
            _web(tmp_path / str(number), logins=bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------------- the command


@pytest.fixture
def listening(monkeypatch: pytest.MonkeyPatch) -> dict:
    """`mtk serve` without really listening: what it makes the app from, and what it asks uvicorn to run."""
    import uvicorn

    import musictoolkit.web.app as web_app

    seen: dict = {"app_options": {}, "run": None}
    real = web_app.create_app

    def spy(**options):
        seen["app_options"] = options
        return real(**options)

    monkeypatch.setattr(web_app, "create_app", spy)
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: seen.update(run={"app": app, **options}))
    monkeypatch.delenv("MTK_TOKEN", raising=False)
    return seen


def _config(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body + '\n[database]\npath = "./data/library.db"\n[logging]\ndir = "./logs"\n', encoding="utf-8")
    return path


def test_serve_passes_the_listed_logins_on_and_says_how_many_and_never_who(tmp_path: Path, listening: dict) -> None:
    body = '[server]\ntailscale_logins = ["You@Example.com", "partner@example.com", "you@example.com"]\n'
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path, body)), "serve"])
    assert result.exit_code == 0, result.output
    assert listening["app_options"]["tailscale_logins"] == ["you@example.com", "partner@example.com"]
    assert "signs in 2 Tailscale accounts without the token" in result.output and "behind `tailscale serve`" in result.output
    assert "example.com" not in result.output and "partner" not in result.output  # (a log is not the place for people's accounts)
    (tmp_path / "one").mkdir()
    one = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path / "one", '[server]\ntailscale_logins = ["you@example.com"]\n')), "serve"])
    assert "signs in 1 Tailscale account without the token" in one.output and "accounts" not in one.output


def test_serve_without_logins_passes_none_on_and_says_nothing_about_it(tmp_path: Path, listening: dict) -> None:
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path)), "serve"])
    assert result.exit_code == 0 and listening["app_options"]["tailscale_logins"] == [] and "Tailscale" not in result.output


@pytest.mark.parametrize("args,body", [
    ([], '[server]\nhost = "0.0.0.0"\nallowed_hosts = ["musicpi.local"]\ntailscale_logins = ["you@example.com"]\n'),
    (["--host", "0.0.0.0"], '[server]\nallowed_hosts = ["musicpi.local"]\ntailscale_logins = ["you@example.com"]\n'),  # (the command line decides where it listens)
    (["--host", "192.168.1.50"], '[server]\nallowed_hosts = ["musicpi.local"]\ntailscale_logins = ["you@example.com"]\n'),
])
def test_serve_refuses_to_trust_the_header_where_a_program_on_the_network_could_write_it(tmp_path: Path, args: list[str], body: str, listening: dict) -> None:
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path, body)), "serve", *args])
    assert result.exit_code == 2 and listening["run"] is None
    assert "Cannot start the server: [server] tailscale_logins needs host = \"127.0.0.1\"" in result.output
    assert not (tmp_path / "server.token").exists() and "you@example.com" not in result.output


def test_serve_may_listen_beyond_this_computer_when_it_signs_nobody_in_with_tailscale(tmp_path: Path, listening: dict) -> None:
    body = '[server]\nhost = "0.0.0.0"\nallowed_hosts = ["musicpi.local"]\ntailscale_logins = []\n'
    assert CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path, body)), "serve"]).exit_code == 0


@pytest.mark.parametrize("written,words", [
    ('tailscale_logins = "you@example.com"', "must be a list"),
    ('tailscale_logins = ["you@example.com", "bob smith"]', "[server] tailscale_logins: 'bob smith' has a space"),
    ('tailscale_logins = [""]', "a login is empty"),
])
def test_serve_refuses_logins_that_are_not_written_as_a_list_of_logins(tmp_path: Path, written: str, words: str, listening: dict) -> None:
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path, f"[server]\n{written}\n")), "serve"])
    assert result.exit_code == 2 and words in result.output and listening["run"] is None


# ---------------------------------------------------------------------------------------------------- the real thing


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port: int, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=headers)
        answer = conn.getresponse()
        return answer.status, {k.lower(): v for k, v in answer.getheaders()}, answer.read()
    finally:
        conn.close()


def test_a_real_server_behind_serves_headers_signs_in_the_listed_account_and_refuses_funnel(tmp_path: Path) -> None:
    port = _free_port()
    config = _config(tmp_path, f'[server]\nport = {port}\nallowed_hosts = ["musicpi.tail1234.ts.net"]\nlogin_days = 30\ntailscale_logins = ["{YOU}"]\n')
    env = {key: value for key, value in os.environ.items() if key != "MTK_TOKEN"}
    env["PYTHONPATH"] = SRC
    process = subprocess.Popen(
        [sys.executable, "-c", "from musictoolkit.cli import app; app()", "--config", str(config), "serve"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=tmp_path,
    )
    # What `tailscale serve` passes on for a request from the person's own phone, in its own words (ipn/ipnlocal/serve.go).
    serve = {
        "Host": "musicpi.tail1234.ts.net", "X-Forwarded-Host": "musicpi.tail1234.ts.net", "X-Forwarded-Proto": "https", "X-Forwarded-For": "100.101.102.103",
        "Tailscale-User-Login": YOU, "Tailscale-User-Name": "You", "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers",
        "Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "User-Agent": BROWSER,
    }
    try:
        deadline = time.monotonic() + 40
        while True:
            try:
                status, _, body = _get(port, "/healthz", {"Host": f"127.0.0.1:{port}"})
                break
            except OSError:
                assert process.poll() is None, process.stderr.read()
                assert time.monotonic() < deadline, "the server did not start"
                time.sleep(0.2)
        assert status == 200 and json.loads(body)["status"] == "ok"
        token = (tmp_path / "server.token").read_text(encoding="utf-8").strip()

        assert _get(port, "/api/about", {"Host": "musicpi.tail1234.ts.net"})[0] == 401  # (a device on the network that Serve knows nothing about)
        status, headers, _ = _get(port, "/api/about", serve)
        cookie = headers["set-cookie"]
        assert status == 200 and cookie.startswith(f"mtk_session={tailnet.session_cookie(token, YOU)};")
        assert "; secure" in cookie.lower() and "max-age=2592000" in cookie.lower() and "httponly" in cookie.lower()  # (over https, for 30 days)
        assert _get(port, "/api/about", {"Host": "musicpi.tail1234.ts.net", "Cookie": cookie.split(";")[0]})[0] == 200
        assert _get(port, "/api/about", {**serve, "Tailscale-User-Login": "mallory@example.com"})[0] == 401
        assert _get(port, "/api/about", {**serve, "Sec-Fetch-Site": "cross-site"})[0] == 401
        status, _, body = _get(port, "/api/about", {**serve, "Tailscale-Funnel-Request": "?1"})
        assert status == 403 and json.loads(body) == {"detail": "this server is not available through Tailscale Funnel"}
        status, _, body = _get(port, "/healthz", {**serve, "Tailscale-Funnel-Request": "?1"})
        assert status == 403
        assert _get(port, "/healthz", {**serve})[0] == 200
    finally:
        if process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)
        try:
            out, err = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    assert "signs in 1 Tailscale account without the token" in out
    assert YOU not in out + err and token not in out + err
