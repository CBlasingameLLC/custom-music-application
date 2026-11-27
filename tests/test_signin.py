"""Signing in from a browser (the page, the form, the link), the limit on wrong tokens, HEAD requests, and that no log or terminal gets the token."""

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
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import musictoolkit.cli as cli
from musictoolkit.cli import app as cli_app
from musictoolkit.web.app import NO_HEAD, ROUTERS
from musictoolkit.web.signin import GuessLimiter, same_token, sign_in_page
from tests.conftest import build_web, scan

SRC = str(Path(__file__).resolve().parent.parent / "src")
TOKEN = "k" * 8 + "T0ken" + "z" * 30


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _web(tmp_path: Path, **options):
    web = build_web(tmp_path, None, token=TOKEN, **options)
    web.clock = Clock()
    web.app.state.limiter._clock = web.clock
    return web


def _wrong_form(web, n: int = 1, client: TestClient | None = None) -> None:
    for number in range(n):
        assert (client or web.client).post("/signin", data={"token": f"wrong-{number}"}).status_code == 401


# ---------------------------------------------------------------------------------------------------- the page and the form


def test_a_browser_without_a_cookie_gets_a_page_with_a_box_for_the_token(tmp_path: Path) -> None:
    web = _web(tmp_path, login_days=90)
    page = web.client.get("/")
    assert page.status_code == 401 and page.headers["content-type"].startswith("text/html") and page.headers["cache-control"] == "no-store"
    assert '<form method="post" action="/signin">' in page.text and 'name="token"' in page.text and 'type="password"' in page.text
    assert "The device then stays signed in for 90 days." in page.text and "mtk token" in page.text
    assert 'class="problem"' not in page.text and TOKEN not in page.text
    assert web.client.get("/api/about").status_code == 401  # (a page, not the app: the app's data still wants the token)


def test_the_page_says_how_long_a_device_stays_signed_in_only_when_that_is_known() -> None:
    assert "stays signed in" not in sign_in_page()
    assert "The device then stays signed in for 30 days." in sign_in_page(login_days=30)
    assert "stays signed in" not in sign_in_page(login_days=0)


def test_what_the_page_says_went_wrong_is_text_and_never_markup() -> None:
    page = sign_in_page(problem="<script>alert(1)</script> & more")
    assert '<p class="problem" role="alert">&lt;script&gt;alert(1)&lt;/script&gt; &amp; more</p>' in page and "<script>" not in page


def test_the_right_token_typed_into_the_form_signs_the_device_in_and_goes_to_the_app(tmp_path: Path) -> None:
    web = _web(tmp_path, login_days=90)
    answer = web.client.post("/signin", data={"token": TOKEN}, follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"] == "/"
    cookie = answer.headers["set-cookie"].lower()
    assert "mtk_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie and "max-age=7776000" in cookie and "path=/" in cookie
    assert web.client.get("/api/about").status_code == 200  # (the cookie rides along)
    assert web.client.get("/").status_code == 200 and web.client.get("/").headers["content-type"].startswith("text/html")


def test_the_cookie_the_form_gives_is_secure_only_over_https_and_ends_with_the_window_without_login_days(tmp_path: Path) -> None:
    web = _web(tmp_path)
    plain = web.client.post("http://testserver/signin", data={"token": TOKEN}, follow_redirects=False).headers["set-cookie"].lower()
    assert "max-age" not in plain and "expires" not in plain and "; secure" not in plain
    other = _web(tmp_path / "b")
    secure = other.client.post("https://testserver/signin", data={"token": TOKEN}, follow_redirects=False).headers["set-cookie"].lower()
    assert "; secure" in secure


def test_a_wrong_token_in_the_form_shows_the_page_again_with_what_is_wrong_and_no_cookie_and_does_not_echo_it(tmp_path: Path) -> None:
    web = _web(tmp_path)
    answer = web.client.post("/signin", data={"token": "nope<b>x"})
    assert answer.status_code == 401 and 'role="alert">That is not the token.</p>' in answer.text
    assert "nope" not in answer.text and "set-cookie" not in answer.headers and answer.headers["cache-control"] == "no-store"
    assert web.client.post("/signin", data={}).status_code == 401  # (nothing typed is not the token either)


def test_opening_the_sign_in_address_by_hand_goes_to_the_app_and_an_app_without_a_token_has_nothing_to_sign_in_to(tmp_path: Path) -> None:
    web = _web(tmp_path)
    moved = web.client.get("/signin", follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"] == "/"
    free = build_web(tmp_path / "free", None)
    answer = free.client.post("/signin", data={"token": "anything"}, follow_redirects=False)
    assert answer.status_code == 303 and "set-cookie" not in answer.headers
    assert free.client.get("/").status_code == 200


def test_a_device_that_is_already_signed_in_is_sent_to_the_app_and_a_typed_token_is_not_looked_at(tmp_path: Path) -> None:
    web = _web(tmp_path)
    web.client.post("/signin", data={"token": TOKEN})
    for _ in range(20):
        answer = web.client.post("/signin", data={"token": "wrong"}, follow_redirects=False)
        assert answer.status_code == 303 and "set-cookie" not in answer.headers
    assert web.app.state.limiter.wait() == 0  # (none of it was counted)


def test_a_form_from_another_site_is_refused_without_looking_at_the_token_or_counting_it(tmp_path: Path) -> None:
    web = _web(tmp_path)
    for _ in range(12):
        refused = web.client.post("/signin", data={"token": TOKEN}, headers={"sec-fetch-site": "cross-site"})
        assert refused.status_code == 403 and "set-cookie" not in refused.headers
    assert web.client.post("/signin", data={"token": TOKEN}, headers={"sec-fetch-site": "same-site"}).status_code == 403  # (a sibling site is another site)
    assert web.app.state.limiter.wait() == 0
    for site in ("same-origin", "none"):  # (the page itself; an address typed or bookmarked)
        fresh = TestClient(web.app)
        assert fresh.post("/signin", data={"token": TOKEN}, headers={"sec-fetch-site": site}, follow_redirects=False).status_code == 303, site


# ---------------------------------------------------------------------------------------------------- the link


def test_the_link_gives_the_page_and_the_cookie_to_a_browser_and_to_a_program_alike(tmp_path: Path) -> None:
    web = _web(tmp_path, login_days=90)
    for mode in ("navigate", "cors", "no-cors", "same-origin", None):  # (not a redirect for any: the address is cleaned by the page, which the desktop window's checks rely on staying as it was)
        fresh = TestClient(web.app)
        headers = {"sec-fetch-mode": mode} if mode else {}
        answer = fresh.get("/", params={"token": TOKEN}, headers=headers, follow_redirects=False)
        assert answer.status_code == 200 and "max-age=7776000" in answer.headers["set-cookie"].lower(), mode
        assert answer.headers["content-type"].startswith("text/html") and answer.headers["cache-control"] == "no-store"
        assert fresh.get("/api/about").status_code == 200  # (the cookie rides along)


def test_a_wrong_token_in_the_link_shows_the_page_and_a_signed_in_device_that_opens_an_old_link_is_not_counted(tmp_path: Path) -> None:
    web = _web(tmp_path)
    refused = web.client.get("/", params={"token": "old"})
    assert refused.status_code == 401 and "That is not the token." in refused.text and "set-cookie" not in refused.headers
    assert web.client.get("/", params={"token": ""}).status_code == 401 and "That is not the token." not in web.client.get("/", params={"token": ""}).text
    web.client.post("/signin", data={"token": TOKEN})
    for _ in range(20):
        old_link = web.client.get("/", params={"token": "old"}, follow_redirects=False)
        assert old_link.status_code == 200 and "set-cookie" not in old_link.headers  # (the app, as it is: no new cookie)
    assert web.app.state.limiter.wait() == 0
    renewed = web.client.get("/", params={"token": TOKEN}, follow_redirects=False)
    assert renewed.status_code == 200 and "mtk_session=" in renewed.headers["set-cookie"]  # (a right link renews the cookie)


# ---------------------------------------------------------------------------------------------------- the limit


def test_after_eight_wrong_tokens_none_is_looked_at_until_the_oldest_is_a_minute_old_and_a_right_one_waits_too(tmp_path: Path) -> None:
    web = _web(tmp_path)
    for number in range(8):
        assert web.client.post("/signin", data={"token": f"wrong-{number}"}).status_code == 401
        web.clock.advance(1)  # wrong tokens at 0, 1 ... 7 seconds; now it is 8
    blocked = web.client.post("/signin", data={"token": TOKEN}, follow_redirects=False)
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "52" and "Too many wrong tokens. Try again in 52 seconds." in blocked.text
    assert "set-cookie" not in blocked.headers and blocked.headers["cache-control"] == "no-store"
    web.clock.advance(51.5)  # 59.5 seconds after the first
    assert web.client.post("/signin", data={"token": TOKEN}, follow_redirects=False).headers["retry-after"] == "1"
    web.clock.advance(0.5)  # exactly a minute after the first: it is forgotten, seven are left, and one more is allowed
    assert web.client.post("/signin", data={"token": TOKEN}, follow_redirects=False).status_code == 303


def test_seven_wrong_tokens_do_not_block_the_eighth_try(tmp_path: Path) -> None:
    web = _web(tmp_path)
    _wrong_form(web, 7)
    assert web.client.post("/signin", data={"token": TOKEN}, follow_redirects=False).status_code == 303


def test_wrong_tokens_from_the_link_the_form_and_a_program_are_counted_together_and_all_three_are_refused_when_there_are_too_many(tmp_path: Path) -> None:
    web = _web(tmp_path)
    for _ in range(3):
        assert web.client.get("/", params={"token": "bad"}).status_code == 401
    _wrong_form(web, 3)
    for _ in range(2):
        assert web.client.get("/api/about", headers={"x-mtk-token": "bad"}).status_code == 401
    link = web.client.get("/", params={"token": TOKEN})
    assert link.status_code == 429 and link.headers["retry-after"] == "60" and "Try again in 60 seconds." in link.text and "set-cookie" not in link.headers
    assert web.client.post("/signin", data={"token": TOKEN}).status_code == 429
    program = web.client.get("/api/about", headers={"x-mtk-token": TOKEN})
    assert program.status_code == 429 and program.headers["retry-after"] == "60" and program.json() == {"detail": "too many wrong tokens: try again in 60 seconds"}
    web.clock.advance(60)
    assert web.client.get("/api/about", headers={"x-mtk-token": TOKEN}).status_code == 200


def test_a_device_that_is_signed_in_is_not_affected_while_wrong_tokens_are_refused(tmp_path: Path) -> None:
    web = _web(tmp_path)
    web.client.post("/signin", data={"token": TOKEN})
    stranger = TestClient(web.app)
    _wrong_form(web, 8, client=stranger)
    assert stranger.post("/signin", data={"token": TOKEN}).status_code == 429
    assert web.client.get("/api/about").status_code == 200
    assert web.client.get("/").status_code == 200


def test_the_cookie_of_an_old_token_is_dropped_and_never_counted(tmp_path: Path) -> None:
    web = _web(tmp_path)
    for _ in range(30):
        stale = web.client.get("/api/about", headers={"cookie": "mtk_session=the-old-token"})
        assert stale.status_code == 401 and stale.json() == {"detail": "unauthorized"} and "retry-after" not in stale.headers  # (nothing to wait for)
        assert stale.headers["set-cookie"].lower().startswith('mtk_session="";') and "max-age=0" in stale.headers["set-cookie"].lower()
    assert web.app.state.limiter.wait() == 0
    assert web.client.post("/signin", data={"token": TOKEN}, follow_redirects=False).status_code == 303
    assert web.client.get("/api/about").status_code == 200  # (and a device whose old cookie was dropped can sign in again)
    bare = TestClient(web.app).get("/api/about")
    assert bare.status_code == 401 and "set-cookie" not in bare.headers  # (no cookie was sent: nothing to drop)


def test_a_right_token_in_the_header_works_whatever_cookie_comes_with_it(tmp_path: Path) -> None:
    web = _web(tmp_path)
    assert web.client.get("/api/about", headers={"x-mtk-token": TOKEN, "cookie": "mtk_session=old"}).status_code == 200


def test_a_token_with_characters_that_are_not_ascii_is_wrong_and_not_an_error(tmp_path: Path) -> None:
    assert same_token("é", "é") and not same_token("é", "e") and not same_token("e", "é") and same_token(TOKEN, TOKEN) and not same_token(TOKEN, TOKEN[:-1])
    web = _web(tmp_path)
    assert web.client.get("/api/about", headers={"x-mtk-token": "é☃".encode()}).status_code == 401
    assert web.client.get("/", params={"token": "é☃"}).status_code == 401
    assert web.client.post("/signin", data={"token": "é☃"}).status_code == 401
    assert web.client.get("/api/about", headers={"cookie": "mtk_session=é".encode("utf-8")}).status_code == 401


def test_a_token_is_the_token_only_letter_for_letter(tmp_path: Path) -> None:
    assert not same_token("ABC", "abc") and not same_token(TOKEN.upper(), TOKEN) and not same_token(" " + TOKEN, TOKEN) and not same_token(TOKEN + " ", TOKEN)
    assert same_token("\ud800", "\ud800") and not same_token("\ud800", TOKEN)  # (not a character a request can carry, and not an error either)
    assert not same_token(TOKEN[:-1], TOKEN) and not same_token(TOKEN[:5], TOKEN) and not same_token("", TOKEN)  # (the start of the token is not the token)
    web = _web(tmp_path)
    assert web.client.post("/signin", data={"token": TOKEN.upper()}).status_code == 401
    assert web.client.post("/signin", data={"token": " " + TOKEN}).status_code == 401
    assert web.client.post("/signin", data={"token": TOKEN[:-1]}).status_code == 401
    assert web.client.get("/", params={"token": TOKEN[:-1]}).status_code == 401
    assert web.client.get("/api/about", headers={"x-mtk-token": TOKEN.upper()}).status_code == 401
    assert web.client.get("/api/about", headers={"x-mtk-token": TOKEN[:-1]}).status_code == 401


def test_the_limiter_forgets_a_wrong_token_when_it_is_exactly_a_window_old() -> None:
    clock = Clock()
    limiter = GuessLimiter(limit=3, window=10, clock=clock)
    assert limiter.wait() == 0
    limiter.wrong()
    clock.advance(1)
    limiter.wrong()
    clock.advance(1)
    assert limiter.wait() == 0  # (two of three)
    limiter.wrong()
    assert limiter.wait() == 8  # (the first was at 0, it is 2: free at 10)
    clock.advance(7.5)
    assert limiter.wait() == 1  # (half a second left is a second: never 0 while it is still closed)
    clock.advance(0.5)
    assert limiter.wait() == 0  # (it is 10: the first is a window old, forgotten)
    limiter.wrong()
    assert limiter.wait() == 1 and (clock.advance(1) or limiter.wait()) == 0  # (wrong at 1, 2 and 10: free at 11)


def test_the_limiter_keeps_no_more_than_it_needs() -> None:
    clock = Clock()
    limiter = GuessLimiter(limit=2, window=5, clock=clock)
    for _ in range(50):
        limiter.wrong()
        clock.advance(10)
    assert len(limiter._wrong) == 1  # (each one made the earlier ones old)


# ---------------------------------------------------------------------------------------------------- HEAD


def test_head_answers_like_get_but_with_no_body_and_wants_the_token_like_get(tmp_path: Path, music_dir: Path) -> None:
    web = build_web(tmp_path, music_dir, token=TOKEN)
    web.client.post("/signin", data={"token": TOKEN})  # (the cookie)
    scan(web)
    track = web.client.get("/api/tracks").json()["items"][0]["id"]
    for path in ("/", "/api/about", f"/api/tracks/{track}/stream", "/healthz", "/static/js/main.js"):
        got, head = web.client.get(path), web.client.head(path)
        assert head.status_code == got.status_code == 200, path
        assert head.headers["content-type"] == got.headers["content-type"], path
        assert head.headers["content-length"] == got.headers["content-length"], path
    stream = web.client.head(f"/api/tracks/{track}/stream")
    assert stream.content == b"" and stream.headers["accept-ranges"] == "bytes" and int(stream.headers["content-length"]) > 1000
    ranged = web.client.head(f"/api/tracks/{track}/stream", headers={"range": "bytes=0-99"})
    assert ranged.status_code == 206 and ranged.headers["content-range"].startswith("bytes 0-99/") and ranged.headers["content-length"] == "100"
    stranger = TestClient(web.app)
    assert stranger.head("/api/about").status_code == 401
    assert stranger.head(f"/api/tracks/{track}/stream").status_code == 401
    assert stranger.head("/").status_code == 401  # (a monitor that wants a 200 asks /healthz)
    assert stranger.head("/healthz").status_code == 200
    assert stranger.head("/", headers={"host": "evil.example"}).status_code == 403


def test_every_route_that_reads_answers_head_except_the_two_that_would_open_a_connection_to_another_server(tmp_path: Path) -> None:
    web = build_web(tmp_path, None, token=TOKEN)
    every = [route for module in ROUTERS for route in module.router.routes] + list(web.app.routes)  # (the routers' own, and the app's)
    routes = [route for route in every if isinstance(route, APIRoute)]
    reading = [route for route in routes if "GET" in route.methods]
    assert len(reading) > 60 and {"/", "/signin", "/healthz", "/api/about"} <= {route.path for route in reading}
    assert {route.path for route in reading if "HEAD" not in route.methods} == NO_HEAD == {
        "/api/radio/stations/{station_id}/stream", "/api/podcasts/episodes/{episode_id}/stream",
    }
    for route in routes:
        if "GET" not in route.methods:
            assert "HEAD" not in route.methods, route.path  # (a route that changes something does not answer HEAD)
    web.client.headers["x-mtk-token"] = TOKEN
    assert web.client.head("/api/radio/stations/1/stream").status_code == 405
    assert web.client.head("/api/podcasts/episodes/1/stream").status_code == 405


# ---------------------------------------------------------------------------------------------------- the token is never written where it can leak


GENERATED = "GeneratedTokenThatMustNeverBePrinted-0123456789"


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text('[database]\npath = "./data/library.db"\n[logging]\ndir = "./logs"\n', encoding="utf-8")
    return path


@pytest.fixture
def dashboard(monkeypatch: pytest.MonkeyPatch) -> dict:
    """`mtk dashboard` without really listening: what it asks uvicorn to run."""
    import secrets

    import uvicorn

    ran: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: ran.update(app=app, **options))
    monkeypatch.setattr(secrets, "token_urlsafe", lambda nbytes=32: GENERATED)
    monkeypatch.delenv("MTK_TOKEN", raising=False)
    return ran


def _run_dashboard(tmp_path: Path):
    return CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path)), "dashboard", "--port", "4999"])


def test_the_dashboard_does_not_print_the_token_when_its_output_is_not_a_terminal_and_says_why_nobody_can_sign_in(tmp_path: Path, dashboard: dict) -> None:
    result = _run_dashboard(tmp_path)  # (CliRunner's output is a pipe, as a service's journal or a log file is)
    assert result.exit_code == 0, result.output
    assert GENERATED not in result.output and "token=" not in result.output
    assert "Music Toolkit is running on 127.0.0.1:4999, but nobody can sign in" in result.output
    assert "Set MTK_TOKEN to a token of your own, or use `mtk serve`." in result.output
    assert (dashboard["host"], dashboard["port"], dashboard["log_level"], dashboard["access_log"]) == ("127.0.0.1", 4999, "warning", False)
    assert dashboard["app"].state.ctx.token == GENERATED  # (it did make one; it only did not say it)


def test_the_dashboard_with_a_token_from_the_environment_never_prints_it_to_a_terminal_or_anywhere_else(tmp_path: Path, dashboard: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MTK_TOKEN", TOKEN)
    for terminal in (False, True):
        monkeypatch.setattr(cli, "_is_terminal", lambda terminal=terminal: terminal)
        result = _run_dashboard(tmp_path)
        assert result.exit_code == 0, result.output
        assert TOKEN not in result.output and "token=" not in result.output
        assert "Music Toolkit is running on 127.0.0.1:4999  (sign in with the token in MTK_TOKEN)" in result.output
        assert dashboard["app"].state.ctx.token == TOKEN and dashboard["access_log"] is False


def test_the_dashboard_prints_the_link_that_signs_in_to_a_person_at_a_terminal_and_only_when_it_made_the_token(tmp_path: Path, dashboard: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_is_terminal", lambda: True)
    result = _run_dashboard(tmp_path)
    assert f"Music Toolkit is running at http://127.0.0.1:4999/?token={GENERATED}  (Ctrl+C to stop)" in result.output
    assert "nobody can sign in" not in result.output


def test_serve_does_not_ask_uvicorn_for_an_access_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    ran: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: ran.update(options))
    monkeypatch.setenv("MTK_TOKEN", TOKEN)
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path)), "serve"])
    assert result.exit_code == 0, result.output
    assert ran["access_log"] is False and TOKEN not in result.output  # (an access log has the address of every request, and the sign-in link's address has the token)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ask(port: int, method: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        answer = conn.getresponse()
        return answer.status, answer.read()
    finally:
        conn.close()


@pytest.mark.parametrize("given", [None, TOKEN])
def test_a_real_dashboard_writes_the_token_nowhere_a_log_could_keep_it(tmp_path: Path, given: str | None) -> None:
    port = _free_port()
    config = _config(tmp_path)
    env = {key: value for key, value in os.environ.items() if key != "MTK_TOKEN"}
    env["PYTHONPATH"] = SRC
    if given:
        env["MTK_TOKEN"] = given
    process = subprocess.Popen(
        [sys.executable, "-c", "from musictoolkit.cli import app; app()", "--config", str(config), "dashboard", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path,
    )
    try:
        deadline = time.monotonic() + 40
        while True:
            try:
                status, body = _ask(port, "GET", "/healthz")
                break
            except OSError:
                assert process.poll() is None, process.stderr.read()
                assert time.monotonic() < deadline, "the app did not start"
                time.sleep(0.2)
        assert status == 200 and json.loads(body)["status"] == "ok"
        assert _ask(port, "HEAD", "/healthz")[0] == 200
        assert _ask(port, "HEAD", "/")[0] == 401 and _ask(port, "GET", "/")[0] == 401
        if given:
            assert _ask(port, "GET", f"/?token={given}")[0] == 200
            assert _ask(port, "GET", "/api/about", {"x-mtk-token": given})[0] == 200
            assert _ask(port, "GET", "/?token=" + "wrong" * 5)[0] == 401  # (a wrong link is not written down either)
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
    everything = out + err
    assert "token=" not in everything and (given is None or given not in everything)
    if given:
        assert "sign in with the token in MTK_TOKEN" in out
    else:
        assert "nobody can sign in" in err
    log = tmp_path / "logs" / "musictoolkit.log"
    assert not log.exists() or (given is None or given not in log.read_text(encoding="utf-8"))


def test_every_router_of_the_app_is_in_the_list_the_app_includes_and_none_twice() -> None:
    import importlib
    import pkgutil

    import musictoolkit.web.routers as package

    found = [module for info in pkgutil.iter_modules(package.__path__) if hasattr(module := importlib.import_module(f"{package.__name__}.{info.name}"), "router")]
    assert len(found) > 20 and set(found) == set(ROUTERS) and len(ROUTERS) == len(set(ROUTERS))


def test_the_output_is_a_terminal_when_the_standard_output_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    class Out:
        def __init__(self, terminal: bool) -> None:
            self.terminal = terminal

        def isatty(self) -> bool:
            return self.terminal

    monkeypatch.setattr(cli.sys, "stdout", Out(True))
    assert cli._is_terminal() is True
    monkeypatch.setattr(cli.sys, "stdout", Out(False))
    assert cli._is_terminal() is False
