"""Who may use /rest: the ways a Subsonic client says who it is, what is told for each way that is wrong, the limit on wrong guesses, and the Tailscale login."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.subsonic_support import Rest, scanned_web

TOKEN = "k" * 8 + "T0ken" + "z" * 30
YOU = "you@example.com"
BROWSER = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def secured(tmp_path: Path, music_dir: Path) -> SimpleNamespace:
    web = scanned_web(tmp_path, music_dir, token=TOKEN, tailscale_logins=[YOU])
    web.clock = Clock()
    web.app.state.limiter._clock = web.clock
    return web


def key_of(web: SimpleNamespace, **extra: str) -> dict:
    return {"v": "1.16.1", "c": "tests", **extra}


def asked(web: SimpleNamespace, name: str = "ping", headers: dict | None = None, **params: str) -> dict:
    response = web.client.get(f"/rest/{name}", params={"f": "json", **params}, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()["subsonic-response"]


def failure(answer: dict) -> tuple[int, str]:
    assert answer["status"] == "failed", answer
    return answer["error"]["code"], answer["error"]["message"]


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()  # noqa: S324 (the protocol's own)


# ------------------------------------------------------------------------------------------------------------------------- the right ways


@pytest.mark.parametrize("auth", ["plain", "enc", "token"])
def test_the_right_password_in_any_of_the_three_forms_of_the_protocol_is_let_in(secured: SimpleNamespace, auth: str) -> None:
    assert Rest(secured.client, password=TOKEN, auth=auth).ok("ping")["status"] == "ok"


def test_an_api_key_alone_is_let_in(secured: SimpleNamespace) -> None:
    assert Rest(secured.client, password=TOKEN, auth="key").ok("getMusicFolders")["musicFolders"]


def test_the_digest_may_be_written_in_capitals(secured: SimpleNamespace) -> None:
    assert asked(secured, **key_of(secured, u="me", s="abc", t=md5(TOKEN + "abc").upper()))["status"] == "ok"


@pytest.mark.parametrize("auth", ["plain", "enc", "token"])
def test_a_user_name_is_whatever_the_client_has_whichever_way_it_signed_in(secured: SimpleNamespace, auth: str) -> None:
    assert Rest(secured.client, password=TOKEN, auth=auth, user="é名").ok("getUser")["user"]["username"] == "é名"
    assert Rest(secured.client, password=TOKEN, auth=auth, user="phone").ok("tokenInfo")["tokenInfo"] == {"username": "phone"}


def test_the_form_of_a_post_carries_the_credentials_as_well(secured: SimpleNamespace) -> None:
    rest = Rest(secured.client, password=TOKEN)
    answer = secured.client.post("/rest/ping", data=rest.params(f="json")).json()["subsonic-response"]
    assert answer["status"] == "ok"
    assert failure(secured.client.post("/rest/ping", data={**rest.params(f="json"), "p": "nope"}).json()["subsonic-response"])[0] == 40


# ------------------------------------------------------------------------------------------------------------------------- the wrong ways


def test_no_credentials_at_all_is_a_missing_parameter(secured: SimpleNamespace) -> None:
    assert failure(asked(secured, **key_of(secured))) == (10, "Required parameter is missing: u and p, or u, t and s, or apiKey")
    assert failure(asked(secured, **key_of(secured, u="me")))[0] == 10


@pytest.mark.parametrize("sent", [{"p": "wrong"}, {"p": ""}, {"p": "enc:" + b"wrong".hex()}, {"p": "enc:zz"}, {"p": "enc:ff"}, {"p": "enc:"}, {"p": "enc:abc"}, {"p": "ENC:" + TOKEN.encode().hex()}])
def test_a_wrong_password_is_error_40_whatever_form_it_is_in(secured: SimpleNamespace, sent: dict) -> None:
    code = failure(asked(secured, **key_of(secured, u="me", **sent)))[0]
    assert code == (10 if sent == {"p": ""} else 40)


def test_a_wrong_digest_is_error_40_and_one_made_with_another_salt_too(secured: SimpleNamespace) -> None:
    assert failure(asked(secured, **key_of(secured, u="me", s="abc", t=md5("wrong" + "abc"))))[0] == 40
    assert failure(asked(secured, **key_of(secured, u="me", s="abc", t=md5(TOKEN + "other"))))[0] == 40
    assert failure(asked(secured, **key_of(secured, u="me", s="abc", t="")))[0] == 10


def test_a_digest_needs_its_salt_and_every_password_needs_its_user_name(secured: SimpleNamespace) -> None:
    assert failure(asked(secured, **key_of(secured, u="me", t=md5(TOKEN))))[1] == "Required parameter is missing: s"
    assert failure(asked(secured, **key_of(secured, p=TOKEN)))[1] == "Required parameter is missing: u"
    assert failure(asked(secured, **key_of(secured, s="abc", t=md5(TOKEN + "abc"))))[1] == "Required parameter is missing: u"


def test_a_wrong_api_key_is_error_44(secured: SimpleNamespace) -> None:
    assert failure(asked(secured, **key_of(secured, apiKey="wrong"))) == (44, "Invalid API key")


@pytest.mark.parametrize("sent", [{"apiKey": TOKEN, "u": "me"}, {"apiKey": TOKEN, "p": TOKEN}, {"p": TOKEN, "t": "x", "u": "me", "s": "y"}, {"apiKey": TOKEN, "t": "x"}])
def test_two_ways_at_once_is_error_43_and_nothing_is_let_in(secured: SimpleNamespace, sent: dict) -> None:
    assert failure(asked(secured, **key_of(secured, **sent))) == (43, "Multiple conflicting authentication mechanisms provided")


# ------------------------------------------------------------------------------------------------------------------------ the version


@pytest.mark.parametrize("version", ["1.16.1", "1.16.0", "1.15.0", "1.1.1", "1.0.0", "1.16", "1", "1.0"])
def test_a_client_that_speaks_this_version_or_an_older_minor_one_is_let_in(secured: SimpleNamespace, version: str) -> None:
    assert asked(secured, **key_of(secured, u="me", p=TOKEN, v=version))["status"] == "ok"


@pytest.mark.parametrize("version,code", [("0.9.0", 20), ("0.99.99", 20), ("1.17.0", 30), ("1.99.0", 30), ("2.0.0", 30), ("10.1.1", 30), ("abc", 0), ("1.x", 0), ("", 10), ("v1", 0), (".", 0)])
def test_a_client_of_another_major_version_or_a_newer_minor_one_is_told_to_upgrade_whichever_has_to(secured: SimpleNamespace, version: str, code: int) -> None:
    sent = key_of(secured, u="me", p=TOKEN)
    sent["v"] = version
    assert failure(asked(secured, **sent))[0] == code


def test_the_digest_is_made_as_the_protocol_asks_and_not_as_something_that_keeps_a_secret(secured: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """(MD5 is what the protocol has: a system that forbids it for security still allows it for this, when it is said that it is not for security.)"""
    import hashlib

    sent = Rest(secured.client, password=TOKEN, auth="token").params(f="json")
    said: list[bool] = []
    real = hashlib.md5

    def md5(data: bytes = b"", *, usedforsecurity: bool = True):
        said.append(usedforsecurity)
        return real(data, usedforsecurity=False)

    monkeypatch.setattr(hashlib, "md5", md5)
    assert secured.client.get("/rest/ping", params=sent).json()["subsonic-response"]["status"] == "ok"
    assert said == [False]


def test_a_version_with_no_minor_number_is_minor_0(secured: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from musictoolkit.web.subsonic import auth

    monkeypatch.setattr(auth, "SERVER_MINOR", 0)
    assert asked(secured, **key_of(secured, u="me", p=TOKEN, v="1"))["status"] == "ok"
    assert asked(secured, **key_of(secured, u="me", p=TOKEN, v="1.0.9"))["status"] == "ok"
    assert failure(asked(secured, **key_of(secured, u="me", p=TOKEN, v="1.1")))[0] == 30


def test_a_version_that_is_not_one_is_said_in_words(secured: SimpleNamespace) -> None:
    assert failure(asked(secured, **key_of(secured, u="me", p=TOKEN, v="abc"))) == (0, "'abc' is not a version of the API")
    assert failure(asked(secured, **key_of(secured, u="me", p=TOKEN, v="1.x")))[1] == "'1.x' is not a version of the API"


def test_a_call_without_the_version_or_the_name_of_the_client_is_a_missing_parameter_before_anything_else(secured: SimpleNamespace) -> None:
    assert failure(asked(secured, c="tests", u="me", p=TOKEN))[1] == "Required parameter is missing: v"
    assert failure(asked(secured, v="1.16.1", u="me", p=TOKEN))[1] == "Required parameter is missing: c"
    assert failure(asked(secured, v="2.0.0", c="tests"))[0] == 30  # (the version is looked at before the password)


def test_the_extensions_may_be_asked_before_anything_else_is_said(secured: SimpleNamespace) -> None:
    answer = asked(secured, "getOpenSubsonicExtensions")
    assert answer["status"] == "ok" and {"name": "apiKeyAuthentication", "versions": [1]} in answer["openSubsonicExtensions"]
    assert failure(asked(secured, "getLicense"))[0] == 10


# --------------------------------------------------------------------------------------------------------------------- the limit on guesses


def test_after_eight_wrong_passwords_not_even_the_right_one_is_looked_at_until_a_minute_has_passed(secured: SimpleNamespace) -> None:
    bad = key_of(secured, u="me", p="nope")
    for _ in range(8):
        assert failure(asked(secured, **bad))[1] == "Wrong username or password"
    code, said = failure(asked(secured, **key_of(secured, u="me", p=TOKEN)))
    assert code == 40 and said == "Too many wrong passwords. Try again in 60 seconds."
    secured.clock.now += 59
    assert failure(asked(secured, **key_of(secured, u="me", p=TOKEN)))[1] == "Too many wrong passwords. Try again in 1 seconds."
    secured.clock.now += 1
    assert asked(secured, **key_of(secured, u="me", p=TOKEN))["status"] == "ok"


def test_every_kind_of_wrong_guess_counts_and_so_do_the_wrong_tokens_of_the_sign_in_page(secured: SimpleNamespace) -> None:
    asked(secured, **key_of(secured, u="me", p="nope"))
    asked(secured, **key_of(secured, u="me", s="abc", t=md5("nope")))
    asked(secured, **key_of(secured, apiKey="nope"))
    for number in range(5):
        assert secured.client.post("/signin", data={"token": f"wrong-{number}"}).status_code == 401
    assert failure(asked(secured, **key_of(secured, u="me", p=TOKEN)))[1].startswith("Too many wrong passwords")
    assert failure(asked(secured, **key_of(secured, apiKey=TOKEN)))[1].startswith("Too many wrong passwords")


def test_what_is_not_a_guess_is_not_counted(secured: SimpleNamespace) -> None:
    for _ in range(20):
        asked(secured, **key_of(secured))  # (nothing offered)
        asked(secured, **key_of(secured, apiKey=TOKEN, u="me"))  # (conflicting)
        asked(secured, **key_of(secured, u="me", t="x"))  # (no salt)
        asked(secured, **key_of(secured, u="me", p=TOKEN))  # (right)
    assert asked(secured, **key_of(secured, u="me", p=TOKEN))["status"] == "ok"


def test_a_wrong_api_key_while_the_limit_is_on_is_refused_like_a_wrong_password(secured: SimpleNamespace) -> None:
    for _ in range(8):
        asked(secured, **key_of(secured, apiKey="nope"))
    assert failure(asked(secured, **key_of(secured, apiKey="nope")))[1].startswith("Too many wrong passwords")


# -------------------------------------------------------------------------------------------------------------- a login Tailscale vouches for


def test_a_login_that_tailscale_vouches_for_needs_no_password_and_is_known_by_the_name_before_the_at_sign(secured: SimpleNamespace) -> None:
    answer = asked(secured, "getUser", headers={"Tailscale-User-Login": YOU}, **key_of(secured))
    assert answer["status"] == "ok" and answer["user"]["username"] == "you"
    answer = asked(secured, "getUser", headers={"Tailscale-User-Login": YOU}, **key_of(secured, u="phone"))
    assert answer["user"]["username"] == "phone"


def test_a_login_that_the_server_does_not_sign_in_still_needs_the_password(secured: SimpleNamespace) -> None:
    assert failure(asked(secured, headers={"Tailscale-User-Login": "mallory@example.com"}, **key_of(secured)))[0] == 10
    assert failure(asked(secured, headers={"Tailscale-User-Login": "mallory@example.com"}, **key_of(secured, u="me", p="nope")))[0] == 40
    assert asked(secured, headers={"Tailscale-User-Login": "mallory@example.com"}, **key_of(secured, u="me", p=TOKEN))["status"] == "ok"


@pytest.mark.parametrize("headers,let_in", [
    ({"user-agent": BROWSER}, False),  # a browser too old to say where a request comes from
    ({"user-agent": BROWSER, "sec-fetch-site": "cross-site"}, False),  # a page of another site
    ({"user-agent": BROWSER, "sec-fetch-site": "same-site"}, False),
    ({"user-agent": BROWSER, "sec-fetch-site": "same-origin"}, True),
    ({"user-agent": BROWSER, "sec-fetch-site": "none"}, True),
    ({"user-agent": "Amperfy/1.0 CFNetwork/1494.0.7 Darwin/23.4.0"}, True),  # a phone app
    ({}, True),
])
def test_a_page_of_another_site_does_not_borrow_the_login_of_the_person_who_has_it_open(secured: SimpleNamespace, headers: dict, let_in: bool) -> None:
    answer = asked(secured, headers={"Tailscale-User-Login": YOU, **headers}, **key_of(secured))
    assert (answer["status"] == "ok") is let_in


def test_a_request_that_came_through_funnel_is_refused_whatever_it_carries(secured: SimpleNamespace) -> None:
    response = secured.client.get("/rest/ping", params={**key_of(secured, u="me", p=TOKEN), "f": "json"}, headers={"Tailscale-Funnel-Request": "?1"})
    assert response.status_code == 403 and response.json() == {"detail": "this server is not available through Tailscale Funnel"}


def test_a_server_that_signs_nobody_in_ignores_the_header(tmp_path: Path, music_dir: Path) -> None:
    web = scanned_web(tmp_path, music_dir, token=TOKEN)
    assert failure(asked(web, headers={"Tailscale-User-Login": YOU}, **key_of(web)))[0] == 10


# ----------------------------------------------------------------------------------------------------------------------- an app without a token


def test_an_app_that_has_no_token_asks_for_no_password_but_still_for_the_version(web: SimpleNamespace) -> None:
    assert asked(web, **key_of(web))["status"] == "ok"
    assert asked(web, **key_of(web, u="me", p="whatever"))["status"] == "ok"
    assert failure(asked(web, c="tests"))[1] == "Required parameter is missing: v"


# ------------------------------------------------------------------------------------------------------------------------------ the logs


def test_a_call_that_fails_unexpectedly_is_answered_as_a_failure_and_the_log_has_the_name_of_the_call_and_not_the_credentials(
    secured: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from musictoolkit.web.subsonic import catalog

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(catalog, "artists", boom)
    with caplog.at_level(logging.ERROR, logger="musictoolkit"):
        answer = asked(secured, "getArtists", **key_of(secured, u="me", p=TOKEN))
    assert failure(answer) == (0, "The server could not answer that")
    assert [(record.name, record.getMessage()) for record in caplog.records] == [("musictoolkit", "Unhandled error answering /rest/getArtists")] and "boom" in caplog.text
    assert TOKEN not in caplog.text and "p=" not in caplog.text
