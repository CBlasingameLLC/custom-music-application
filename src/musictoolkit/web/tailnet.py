"""Signing in with the Tailscale identity, so that a device on the person's own tailnet needs no token.

`tailscale serve` (Tailscale's reverse proxy, which gives the app its https address) says which Tailscale account a request came from:
it puts the account's login in the header `Tailscale-User-Login`, after deleting any such header that the sender wrote (underscore
spellings too). It sets none for a request from a tagged device, and none for a request that came through Funnel (the public
internet), which it marks with `Tailscale-Funnel-Request` instead. (https://tailscale.com/kb/1312/serve; `ipn/ipnlocal/serve.go` in
Tailscale's source.)

A server that listens on this computer only, behind `tailscale serve`, can therefore sign in the accounts it was told about
(`[server] tailscale_logins`) without a token. What makes that safe, and what this module decides:

  * Only a server that listens on this computer only may believe the header: whatever can reach the app directly can write one.
    (`mtk serve` refuses the setting on any other address.)
  * A page of another site can make the browser send a request to the app, and the request then carries the person's identity too.
    So a browser's request counts only when the browser says (Fetch Metadata, `Sec-Fetch-Site`) that the app's own page, or the
    address the person typed or tapped, made it. A program (a phone app that speaks the Subsonic protocol) sends no such header and no
    browser's name, and counts. A browser too old to send the header is told by its name, and does not.
  * What the app gives back is a cookie that is good only while the login is still on the list and the token is the one it was made
    with: taking an account off the list, or changing the token, ends every session made for it at once.
  * A request that came through Funnel is refused whatever it asks for: this server is for a private network.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from email.header import decode_header, make_header

from starlette.datastructures import Headers

from musictoolkit.web.signin import same_token

LOGIN_HEADER = "tailscale-user-login"
FUNNEL_HEADER = "tailscale-funnel-request"
MAX_LOGIN = 254  # (the longest an email address can be; a login is not longer)
COOKIE_PREFIX = "tailnet."  # (what tells the cookie of a Tailscale sign-in from the token itself, which is what the other sign-in keeps)
_SESSION_LABEL = b"musictoolkit sign-in with tailscale\0"


def clean_logins(entries: Iterable[str]) -> list[str]:
    """The logins as they are compared (lower case), each once, in the order given. Raises ValueError, naming the entry, for one that
    is not a login: not text, empty, with a space or a control character in it, or longer than any login is."""
    out: list[str] = []
    for raw in entries:
        if not isinstance(raw, str):
            raise ValueError(f"{raw!r} is not a login: write each one in quotes")
        text = raw.strip()
        if not text:
            raise ValueError("a login is empty")
        if len(text) > MAX_LOGIN:
            raise ValueError(f"{text[:20]!r}... is too long to be a login")
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError(f"{raw!r} has a space or a control character in it")
        key = text.lower()
        if key not in out:
            out.append(key)
    return out


def login_in(headers: Headers) -> str | None:
    """The login Tailscale wrote in the request, as it wrote it (a name with letters that are not ASCII comes RFC 2047 encoded: that is
    undone). None when there is none, when there are two (Tailscale writes one), or when it cannot be read."""
    values = headers.getlist(LOGIN_HEADER)
    if len(values) != 1 or len(values[0]) > 4 * MAX_LOGIN:
        return None
    try:
        text = str(make_header(decode_header(values[0]))).strip()
    except Exception:  # (a header that does not parse as encoded text: not a login)
        return None
    return text if text and len(text) <= MAX_LOGIN else None


def from_own_page(headers: Headers) -> bool:
    """Did the app's own page, or the person typing or tapping its address, make this request (and not a page of another site)?
    A browser says so in `Sec-Fetch-Site`. Without it the request is from a program, which counts, or from a browser too old to say,
    which does not (a browser's name starts with "Mozilla/")."""
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site.strip().lower() in ("same-origin", "none")
    return not headers.get("user-agent", "").lstrip().lower().startswith("mozilla/")  # (a browser too old to say sends its name so: not trusted; a program's name is its own)


def through_funnel(headers: Headers) -> bool:
    """Did the request come through Tailscale Funnel, that is from the public internet? (Tailscale writes the header for those and
    deletes it from any other request.)"""
    return FUNNEL_HEADER in headers


@dataclass(frozen=True)
class Seen:
    """What a request says about who is asking, by Tailscale's word."""

    login: str | None  # the login Tailscale put in it, as written (None: no login)
    allowed: bool  # it is one of the logins this server signs in
    own_page: bool  # the app's own page made the request (or a program), not a page of another site

    @property
    def vouched(self) -> bool:
        """Tailscale vouches for the request, and the server accepts it: signed in."""
        return self.login is not None and self.allowed and self.own_page


def look(headers: Headers, allowed: frozenset[str]) -> Seen:
    """`allowed`: the logins the server signs in, lower case (see `clean_logins`)."""
    login = login_in(headers)
    return Seen(login=login, allowed=login is not None and login.lower() in allowed, own_page=from_own_page(headers))


def session_cookie(token: str, login: str) -> str:
    """The cookie value of a device signed in as `login`: made from the token, so it is no good once the token is changed, and from the
    login, so it is no good for another one. It is not the token (a cookie that leaked is no use to the other sign-in)."""
    key = token.encode("utf-8", "surrogatepass")
    return COOKIE_PREFIX + hmac.new(key, _SESSION_LABEL + login.lower().encode("utf-8"), hashlib.sha256).hexdigest()


def cookie_login(value: str, token: str, allowed: Iterable[str]) -> str | None:
    """The login on the list that this cookie was made for, or None (not one of ours, another token's, or a login that is off the list)."""
    for login in allowed:
        if same_token(value, session_cookie(token, login)):  # (in constant time: how far a wrong value matches is not told by how long it takes)
            return login
    return None
