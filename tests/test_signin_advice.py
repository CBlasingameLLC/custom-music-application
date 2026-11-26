"""What Settings says about how a device signs in (static/js/install.js signInAdvice, run under node: it is a plain function)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from musictoolkit.web.app import STATIC_DIR

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
import fs from 'node:fs';
const source = fs.readFileSync(process.argv[2], 'utf8');
const { signInAdvice } = await import('data:text/javascript,' + encodeURIComponent(source));
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = {};
for (const [name, session] of Object.entries(cases)) out[name] = signInAdvice(session);
console.log(JSON.stringify(out));
"""


def _session(by: str, *, on: bool, login: str | None, allowed: bool, own_page: bool = True) -> dict:
    return {"by": by, "tailscale": {"on": on, "login": login, "allowed": allowed, "own_page": own_page}}


ODD = 'a"b\\c@example.com'
CASES = {
    "nothing_yet": None,
    "open_app": _session("open", on=False, login=None, allowed=False),
    "open_app_that_is_named": _session("open", on=True, login="you@example.com", allowed=True),
    "token_nobody_named": _session("token", on=False, login=None, allowed=False),
    "token_nobody_named_on": _session("token", on=True, login=None, allowed=False),
    "no_tailscale_part": {"by": "token"},
    "tailscale_listed": _session("tailscale", on=True, login="you@example.com", allowed=True),
    "tailscale_cookie_only": _session("tailscale", on=True, login=None, allowed=False),
    "token_but_listed": _session("token", on=True, login="you@example.com", allowed=True),
    "unlisted": _session("token", on=True, login="mallory@example.com", allowed=False),
    "offer": _session("token", on=False, login="you@example.com", allowed=False),
    "offer_odd": _session("token", on=False, login=ODD, allowed=False),
    "offer_unicode": _session("token", on=False, login="jürgen@example.com", allowed=False),
    "offer_astral": _session("token", on=False, login="名前😀@example.com", allowed=False),
    "listed_but_off": _session("token", on=False, login="you@example.com", allowed=True),  # (cannot happen: a server that is off lists no one)
}


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict:
    folder = tmp_path_factory.mktemp("signin_advice")
    (folder / "harness.mjs").write_text(HARNESS, encoding="utf-8")
    (folder / "cases.json").write_text(json.dumps(CASES), encoding="utf-8")
    done = subprocess.run([NODE, str(folder / "harness.mjs"), str(Path(STATIC_DIR) / "js" / "install.js"), str(folder / "cases.json")], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("name", ["nothing_yet", "open_app", "open_app_that_is_named"])
def test_an_app_without_a_token_or_a_page_that_has_not_asked_yet_has_nothing_to_say(results: dict, name: str) -> None:
    assert results[name] == {"state": "open"}


@pytest.mark.parametrize("name", ["token_nobody_named", "token_nobody_named_on", "no_tailscale_part"])
def test_a_device_signed_in_with_the_token_that_tailscale_did_not_name_is_told_to_open_the_servers_own_address(results: dict, name: str) -> None:
    assert results[name] == {"state": "token", "by": "token"}


def test_a_device_tailscale_signed_in_earlier_is_not_said_to_be_signed_in_with_the_token_when_this_visit_names_no_one(results: dict) -> None:
    assert results["tailscale_cookie_only"] == {"state": "remembered", "by": "tailscale"}


def test_a_listed_account_is_said_to_be_signed_in_by_tailscale_however_this_device_got_in(results: dict) -> None:
    assert results["tailscale_listed"] == {"state": "tailscale", "by": "tailscale", "login": "you@example.com"}
    assert results["token_but_listed"] == {"state": "tailscale", "by": "token", "login": "you@example.com"}


def test_an_account_the_server_does_not_sign_in_is_named_with_the_entry_that_would_add_it(results: dict) -> None:
    assert results["unlisted"] == {"state": "unlisted", "by": "token", "login": "mallory@example.com", "entry": '"mallory@example.com"'}


def test_a_server_that_signs_nobody_in_is_offered_the_line_that_would_sign_this_account_in(results: dict) -> None:
    assert results["offer"] == {
        "state": "offer", "by": "token", "login": "you@example.com", "entry": '"you@example.com"', "line": 'tailscale_logins = ["you@example.com"]',
    }
    assert results["listed_but_off"]["state"] == "offer"  # (whether the server signs this account in is decided by `on` first)


@pytest.mark.parametrize("name,login", [("offer", "you@example.com"), ("offer_odd", ODD), ("offer_unicode", "jürgen@example.com"), ("offer_astral", "名前😀@example.com")])
def test_the_line_and_the_entry_are_toml_that_means_the_login(results: dict, name: str, login: str) -> None:
    advice = results[name]
    assert tomllib.loads(advice["line"]) == {"tailscale_logins": [login]}
    assert tomllib.loads(f"x = [{advice['entry']}]") == {"x": [login]}
    assert advice["line"] == f"tailscale_logins = [{advice['entry']}]"
