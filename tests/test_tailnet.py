"""Signing in with the Tailscale identity: what makes a request count, the cookie that comes of it, the page that explains it, the checks at start."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from starlette.datastructures import Headers

from musictoolkit import server as server_ops
from musictoolkit.config import Config, load_config, save_config
from musictoolkit.web import tailnet
from musictoolkit.web.signin import sign_in_page, tailnet_note

BROWSER = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
PROGRAM = "Amperfy/1.0 CFNetwork/1494.0.7 Darwin/23.4.0"


def _headers(**fields: str) -> Headers:
    """Headers from keyword names (an underscore is a hyphen: tailscale_user_login)."""
    return Headers({name.replace("_", "-"): value for name, value in fields.items()})


# ---------------------------------------------------------------------------------------------------- the logins that are listed


def test_logins_are_compared_in_lower_case_each_once_in_the_order_given() -> None:
    assert tailnet.clean_logins(["Alice@Example.com", "bob@example.com", "ALICE@example.COM", "  carol@example.com  "]) == [
        "alice@example.com", "bob@example.com", "carol@example.com",
    ]
    assert tailnet.clean_logins([]) == [] and tailnet.clean_logins(()) == []
    assert tailnet.clean_logins(["Jürgen@Example.com"]) == ["jürgen@example.com"]


@pytest.mark.parametrize("entry,hint", [
    ("", "a login is empty"),
    ("   ", "a login is empty"),
    ("alice smith@example.com", "has a space or a control character"),
    ("alice\t@example.com", "has a space or a control character"),
    ("alice\n@example.com", "has a space or a control character"),
    ("alice\x7f@example.com", "has a space or a control character"),
    ("alice\x00@example.com", "has a space or a control character"),
    ("a" * 255, "too long to be a login"),
    (5, "is not a login: write each one in quotes"),
    (None, "is not a login: write each one in quotes"),
    (["alice@example.com"], "is not a login: write each one in quotes"),
])
def test_an_entry_that_is_not_a_login_is_refused_and_named(entry: object, hint: str) -> None:
    with pytest.raises(ValueError) as refused:
        tailnet.clean_logins(["fine@example.com", entry])  # type: ignore[list-item]
    assert hint in str(refused.value)


def test_a_login_can_be_as_long_as_any_login_is_and_not_longer() -> None:
    assert tailnet.clean_logins(["a" * 254]) == ["a" * 254]
    with pytest.raises(ValueError, match="too long"):
        tailnet.clean_logins(["a" * 255])
    assert tailnet.clean_logins([" " + "a" * 254 + " "]) == ["a" * 254]  # (the spaces around it are not part of it)


# ---------------------------------------------------------------------------------------------------- the header Tailscale writes


def test_the_login_is_what_tailscale_wrote_with_its_capitals_kept() -> None:
    assert tailnet.login_in(_headers(tailscale_user_login="Alice@Example.com")) == "Alice@Example.com"
    assert tailnet.login_in(_headers(tailscale_user_login="  alice@example.com ")) == "alice@example.com"
    assert tailnet.login_in(Headers({"Tailscale-User-Login": "alice@example.com"})) == "alice@example.com"  # (a header's name has no case)


@pytest.mark.parametrize("sent", ["=?utf-8?q?J=C3=BCrgen@example.com?=", "=?utf-8?b?SsO8cmdlbkBleGFtcGxlLmNvbQ==?=", "=?UTF-8?Q?J=C3=BCrgen@example.com?="])
def test_a_login_with_letters_that_are_not_ascii_comes_encoded_and_is_decoded(sent: str) -> None:
    assert tailnet.login_in(_headers(tailscale_user_login=sent)) == "Jürgen@example.com"


@pytest.mark.parametrize("headers", [
    Headers({}),
    Headers({"tailscale-user-login": ""}),
    Headers({"tailscale-user-login": "   "}),
    Headers({"tailscale-user-name": "Alice", "tailscale-user-profile-pic": "https://example.com/a.png"}),  # (the other two headers say nothing about who it is)
    Headers({"tailscale_user_login": "alice@example.com"}),  # (the underscore spelling: Tailscale deletes it from what it passes on, and it is not read here)
    Headers({"x-tailscale-user-login": "alice@example.com"}),
    Headers({"tailscale-user-login": "=?bogus-charset?q?abc?="}),  # (does not decode)
    Headers({"tailscale-user-login": "a" * 1017}),  # (longer than any login can be written, even encoded)
    Headers({"tailscale-user-login": "=?utf-8?q?" + "a" * 300 + "?="}),  # (decodes to something longer than any login is)
    Headers(raw=[(b"tailscale-user-login", b"alice@example.com"), (b"tailscale-user-login", b"bob@example.com")]),  # (Tailscale writes one: two is somebody else's doing)
    Headers(raw=[(b"tailscale-user-login", b"alice@example.com"), (b"tailscale-user-login", b"alice@example.com")]),  # (even the same twice)
])
def test_no_login_is_read_from_anything_that_is_not_one_header_that_holds_one(headers: Headers) -> None:
    assert tailnet.login_in(headers) is None


def test_the_longest_login_that_is_read_is_the_longest_a_login_can_be() -> None:
    assert tailnet.login_in(_headers(tailscale_user_login="a" * 254)) == "a" * 254
    assert tailnet.login_in(_headers(tailscale_user_login="a" * 255)) is None
    assert tailnet.login_in(_headers(tailscale_user_login="a" * 1016)) is None  # (and one that long is not even decoded)


@pytest.mark.parametrize("fields", [{"tailscale_funnel_request": "?1"}, {"tailscale_funnel_request": ""}, {"Tailscale-Funnel-Request": "no"}])
def test_a_request_that_came_through_funnel_is_known_by_the_header_tailscale_writes_whatever_it_holds(fields: dict[str, str]) -> None:
    assert tailnet.through_funnel(Headers({name.replace("_", "-"): value for name, value in fields.items()}))


@pytest.mark.parametrize("fields", [{}, {"tailscale_user_login": "alice@example.com"}, {"x_funnel": "?1"}, {"tailscale_funnel": "?1"}])
def test_a_request_without_that_header_did_not_come_through_funnel(fields: dict[str, str]) -> None:
    assert not tailnet.through_funnel(_headers(**fields))


# ---------------------------------------------------------------------------------------------------- who made the request


@pytest.mark.parametrize("site,own", [
    ("same-origin", True), ("none", True), ("SAME-ORIGIN", True), (" none ", True),
    ("same-site", False), ("cross-site", False), ("CROSS-SITE", False), ("", False), ("bogus", False), ("same-origin, cross-site", False),
])
def test_a_browser_that_says_where_the_request_came_from_is_believed_and_only_the_apps_own_page_and_the_address_bar_count(site: str, own: bool) -> None:
    assert tailnet.from_own_page(_headers(sec_fetch_site=site)) is own
    assert tailnet.from_own_page(_headers(sec_fetch_site=site, user_agent=BROWSER)) is own
    assert tailnet.from_own_page(_headers(sec_fetch_site=site, user_agent=PROGRAM)) is own  # (what it says wins over what it is called)


@pytest.mark.parametrize("agent,own", [
    (BROWSER, False), ("Mozilla/5.0", False), ("mozilla/5.0", False), ("  Mozilla/4.0 (compatible)", False),
    (PROGRAM, True), ("curl/8.5.0", True), ("python-requests/2.31.0", True), ("okhttp/4.12.0", True), ("", True), ("Not-Mozilla/1.0", True),
])
def test_without_that_header_a_browser_is_told_by_its_name_and_does_not_count_while_a_program_does(agent: str, own: bool) -> None:
    assert tailnet.from_own_page(_headers(user_agent=agent)) is own
    assert tailnet.from_own_page(_headers()) is True  # (no name at all: a program)


def test_what_a_request_says_about_who_made_it_needs_all_three_to_sign_it_in() -> None:
    allowed = frozenset({"alice@example.com"})
    own = {"sec_fetch_site": "same-origin"}
    seen = tailnet.look(_headers(tailscale_user_login="Alice@Example.com", **own), allowed)
    assert seen == tailnet.Seen(login="Alice@Example.com", allowed=True, own_page=True) and seen.vouched  # (capitals do not matter; the login is kept as written)
    elsewhere = tailnet.look(_headers(tailscale_user_login="alice@example.com", sec_fetch_site="cross-site"), allowed)
    assert (elsewhere.allowed, elsewhere.own_page, elsewhere.vouched) == (True, False, False)
    stranger = tailnet.look(_headers(tailscale_user_login="mallory@example.com", **own), allowed)
    assert (stranger.login, stranger.allowed, stranger.own_page, stranger.vouched) == ("mallory@example.com", False, True, False)
    nobody = tailnet.look(_headers(**own), allowed)
    assert (nobody.login, nobody.allowed, nobody.own_page, nobody.vouched) == (None, False, True, False)
    off = tailnet.look(_headers(tailscale_user_login="alice@example.com", **own), frozenset())
    assert (off.login, off.allowed, off.vouched) == ("alice@example.com", False, False)  # (no list: the header is only read)


# ---------------------------------------------------------------------------------------------------- the cookie


TOKEN = "k" * 8 + "T0ken" + "z" * 30


def test_the_cookie_of_a_login_is_made_from_the_token_and_the_login_and_shows_neither() -> None:
    value = tailnet.session_cookie(TOKEN, "alice@example.com")
    assert value.startswith("tailnet.") and len(value) == len("tailnet.") + 64 and int(value.removeprefix("tailnet."), 16) >= 0
    assert value == tailnet.session_cookie(TOKEN, "alice@example.com") == tailnet.session_cookie(TOKEN, "Alice@EXAMPLE.com")  # (the same every time, for any capitals)
    assert value != tailnet.session_cookie(TOKEN, "bob@example.com") and value != tailnet.session_cookie(TOKEN + "x", "alice@example.com")
    assert TOKEN not in value and "alice" not in value and value != TOKEN
    assert tailnet.session_cookie("é☃" + TOKEN, "alice@example.com").startswith("tailnet.")  # (a token with letters that are not ASCII is still one)


def test_a_cookie_is_good_for_the_login_it_was_made_for_while_that_login_is_on_the_list() -> None:
    cookie = tailnet.session_cookie(TOKEN, "bob@example.com")
    assert tailnet.cookie_login(cookie, TOKEN, ["alice@example.com", "bob@example.com", "carol@example.com"]) == "bob@example.com"
    assert tailnet.cookie_login(cookie, TOKEN, ["bob@example.com"]) == "bob@example.com"
    assert tailnet.cookie_login(cookie, TOKEN, ["alice@example.com", "carol@example.com"]) is None  # (taken off the list)
    assert tailnet.cookie_login(cookie, TOKEN, []) is None  # (no list at all)
    assert tailnet.cookie_login(cookie, TOKEN + "!", ["bob@example.com"]) is None  # (the token was changed)
    assert tailnet.cookie_login(tailnet.session_cookie(TOKEN, "alice@example.com"), TOKEN, ["bob@example.com"]) is None  # (another login's)


@pytest.mark.parametrize("value", [
    "", TOKEN, "tailnet.", "tailnet.0", "tailnet." + "0" * 64, "TAILNET." + "0" * 64, "tailnet.é" + "0" * 63, "tailnet.\udcff", " ",
])
def test_a_cookie_that_is_not_one_this_server_made_is_nobodys(value: str) -> None:
    assert tailnet.cookie_login(value, TOKEN, ["alice@example.com"]) is None


def test_the_cookie_of_a_login_has_to_match_whole_and_not_in_part() -> None:
    cookie = tailnet.session_cookie(TOKEN, "alice@example.com")
    assert tailnet.cookie_login(cookie[:-1], TOKEN, ["alice@example.com"]) is None
    assert tailnet.cookie_login(cookie + "0", TOKEN, ["alice@example.com"]) is None
    assert tailnet.cookie_login(cookie[:-1] + ("0" if cookie[-1] != "0" else "1"), TOKEN, ["alice@example.com"]) is None


# ---------------------------------------------------------------------------------------------------- what the sign-in page says


def test_the_page_says_nothing_about_tailscale_when_tailscale_named_no_one() -> None:
    assert tailnet_note(None, on=False, allowed=False) == "" and tailnet_note(None, on=True, allowed=True) == ""
    page = sign_in_page()
    assert "Tailscale" not in page and "tailscale_logins" not in page
    assert sign_in_page(tailscale_login=None, tailscale_on=True) == page


def test_a_server_that_signs_nobody_in_shows_the_line_that_would_sign_this_device_in() -> None:
    page = sign_in_page(tailscale_login="alice@example.com")
    assert "This server can sign this device in through Tailscale, with no token." in page and "<strong>alice@example.com</strong>" in page
    assert "<pre>tailscale_logins = [&quot;alice@example.com&quot;]</pre>" in page
    assert "<code>[server]</code> section of config.toml" in page and "restart it" in page
    assert page.index('<form method="post" action="/signin">') < page.index("Tailscale")  # (the token form is first: it is what works now)


@pytest.mark.parametrize("login", ['alice@example.com', 'a"b@example.com', "a\\b@example.com", "jürgen@example.com", "a'b@example.com", "名前@example.com"])
def test_the_line_in_the_page_is_a_line_of_toml_that_means_the_login(login: str) -> None:
    import html
    import re

    page = sign_in_page(tailscale_login=login)
    shown = html.unescape(re.search(r"<pre>(.*?)</pre>", page, re.S).group(1))  # type: ignore[union-attr]
    assert tomllib.loads(shown)["tailscale_logins"] == [login]
    assert tailnet.clean_logins(tomllib.loads(shown)["tailscale_logins"]) == [login.casefold()]  # (and `mtk serve` takes it)


def test_a_login_is_shown_as_text_and_never_as_markup() -> None:
    evil = "<script>alert(1)</script>@example.com"
    for page in (sign_in_page(tailscale_login=evil), sign_in_page(tailscale_login=evil, tailscale_on=True), sign_in_page(tailscale_login=evil, tailscale_on=True, tailscale_allowed=True)):
        assert "<script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;@example.com" in page


def test_a_server_that_signs_others_in_says_why_this_device_is_not() -> None:
    page = sign_in_page(tailscale_login="mallory@example.com", tailscale_on=True)
    assert "<strong>mallory@example.com</strong>" in page and "This server does not sign in that account." in page
    assert "tailscale_logins" in page and "<pre>" not in page  # (no line to copy: it is not for anyone to let in on the page's word)
    assert "This server can sign this device in" not in page


def test_a_listed_account_that_is_not_signed_in_is_told_the_page_was_opened_from_another_site() -> None:
    page = sign_in_page(tailscale_login="alice@example.com", tailscale_on=True, tailscale_allowed=True)
    assert "which this server signs in without the token, but this page was opened from another site" in page
    assert "Open the server's address itself" in page and "<pre>" not in page and "does not sign in that account" not in page


# ---------------------------------------------------------------------------------------------------- the checks at start


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1", "localhost", " LOCALHOST "])
def test_the_logins_are_taken_when_the_server_listens_on_this_computer_only(bind: str) -> None:
    assert server_ops.tailscale_logins(bind, ["Alice@Example.com", "bob@example.com"]) == ["alice@example.com", "bob@example.com"]
    assert server_ops.tailscale_logins(bind, ("alice@example.com",)) == ["alice@example.com"]


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.168.1.50", "musicpi.local", "100.101.102.103"])
def test_the_logins_are_refused_where_a_program_on_the_network_could_write_the_header(bind: str) -> None:
    with pytest.raises(server_ops.ServeError) as refused:
        server_ops.tailscale_logins(bind, ["alice@example.com"])
    message = str(refused.value)
    assert f"it would listen on {bind}" in message and 'host = "127.0.0.1"' in message and "tailscale serve" in message
    assert "alice@example.com" not in message  # (what is refused is the address, not a person)


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "127.0.0.1"])
@pytest.mark.parametrize("nothing", [[], (), None, ""])
def test_no_logins_is_no_setting_and_asks_nothing_of_the_address(bind: str, nothing: object) -> None:
    assert server_ops.tailscale_logins(bind, nothing) == []


@pytest.mark.parametrize("entries", ["alice@example.com", 5, {"alice@example.com": 1}, True])
def test_logins_written_as_anything_but_a_list_are_refused_with_how_to_write_them(entries: object) -> None:
    with pytest.raises(server_ops.ServeError) as refused:
        server_ops.tailscale_logins("127.0.0.1", entries)
    assert 'tailscale_logins = ["you@example.com"]' in str(refused.value) and "must be a list" in str(refused.value)


def test_a_login_that_is_not_one_stops_the_server_and_says_where_it_is() -> None:
    with pytest.raises(server_ops.ServeError) as refused:
        server_ops.tailscale_logins("127.0.0.1", ["alice@example.com", "bob smith"])
    assert str(refused.value).startswith("[server] tailscale_logins: 'bob smith' has a space or a control character")
    with pytest.raises(server_ops.ServeError, match="in quotes"):
        server_ops.tailscale_logins("127.0.0.1", [7])
    with pytest.raises(server_ops.ServeError, match="empty"):
        server_ops.tailscale_logins("0.0.0.0", [""])  # (what is wrong with the list is said before where it would listen)


def test_the_setting_has_a_safe_default_is_read_and_saved_and_an_older_config_still_loads(tmp_path: Path) -> None:
    assert Config().server.tailscale_logins == []
    path = tmp_path / "config.toml"
    path.write_text('[server]\ntailscale_logins = ["Alice@Example.com", "bob@example.com"]\nhost = "127.0.0.1"\n', encoding="utf-8")
    loaded = load_config(path)
    assert loaded.server.tailscale_logins == ["Alice@Example.com", "bob@example.com"]  # (as written; they are cleaned when the server starts)
    save_config(path, loaded)
    assert load_config(path).server.tailscale_logins == ["Alice@Example.com", "bob@example.com"]
    path.write_text('[server]\nport = 4533\n', encoding="utf-8")
    assert load_config(path).server.tailscale_logins == []
    assert Config().server.tailscale_logins is not Config().server.tailscale_logins  # (each config has a list of its own)
