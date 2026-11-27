"""Signing in from a browser: the page that asks for the token, and the small limit on wrong ones.

A device that has the sign-in link (`mtk token --url ...`) is signed in by opening it. A device that has only the address, or
whose cookie ended, gets this page instead and pastes the token. Either way what it gets back is the same cookie. (A device whose browser lost
the cookie but kept the key that the app saved is signed in by the page's script before anyone is asked: see remember.py.)

The page also says, in a fold-out, what the request for it carried (a cookie or not, and where the browser says the request came from), so that
"it asked again" is something a person can read the reason of. Nothing of a request is copied into the page but a few words that a browser
writes in its own headers.
"""

from __future__ import annotations

import hmac
import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import escape


def same_token(given: str, token: str) -> bool:
    """Is `given` the token? Compared in constant time, and on bytes (the comparison of two strings fails for a character that is not ASCII)."""
    return hmac.compare_digest(given.encode("utf-8", "surrogatepass"), token.encode("utf-8", "surrogatepass"))


class GuessLimiter:
    """After `limit` wrong tokens within `window` seconds, no token is looked at until the oldest of them is `window` seconds old
    (a right one is refused too: that is what makes guessing slow). It counts the tokens that someone typed, followed in a link or
    sent from a program; a device that is already signed in is never counted, and neither is the cookie it keeps sending after the
    token was changed."""

    def __init__(self, limit: int = 8, window: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._wrong: deque[float] = deque()
        self._lock = threading.Lock()

    def _forget_old(self, now: float) -> None:
        while self._wrong and self._wrong[0] + self.window <= now:
            self._wrong.popleft()

    def wait(self) -> int:
        """Whole seconds until a token is looked at again; 0 when one is looked at now."""
        with self._lock:
            now = self._clock()
            self._forget_old(now)
            if len(self._wrong) < self.limit:
                return 0
            return math.ceil(self._wrong[0] + self.window - now)  # (above zero: the oldest was not forgotten, so it is less than a window old)

    def wrong(self) -> None:
        with self._lock:
            now = self._clock()
            self._forget_old(now)
            self._wrong.append(now)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0d0f14"><link rel="manifest" href="/static/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Music Kit">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>Music Toolkit</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, sans-serif; max-width: 30rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.5; }}
form {{ display: flex; flex-direction: column; gap: .5rem; margin: 1.25rem 0; }}
input, button {{ font: inherit; padding: .6rem .75rem; }}
.problem {{ color: #b3261e; font-weight: 600; }}
.note {{ opacity: .75; font-size: .9rem; }}
pre {{ overflow-x: auto; padding: .5rem .75rem; border: 1px solid; border-radius: .4rem; user-select: all; -webkit-user-select: all; }}
details.note summary {{ cursor: pointer; }}
</style></head>
<body>
<h1>Music Toolkit</h1>
<p>This server shows its music only to a device that has its access token.</p>
{problem}
<form method="post" action="/signin">
<label for="token">Access token</label>
<input id="token" name="token" type="password" autocomplete="current-password" autocapitalize="off" spellcheck="false" required autofocus>
<button type="submit">Sign in</button>
</form>
{restore}
{tailnet}
<p class="note">{where}</p>
{why}
</body></html>
"""


_WORD = re.compile(r"[a-z]{1,12}(-[a-z]{1,12})?")


def _word(value: str | None) -> str:
    """What a browser wrote in one of its `Sec-Fetch-*` headers, as a word the page and the log may repeat: those headers hold a few lower-case words, and
    anything else (not from a browser) is "other". "" when the header was not there."""
    value = (value or "").strip().lower()
    return value if _WORD.fullmatch(value) else ("other" if value else "")


@dataclass(frozen=True)
class Arrival:
    """How a request for the page that asks for the token came, as far as it tells why this device is asked: whether a sign-in cookie came with it ("none", or
    "refused": one came and the server does not take it), and what the browser said about where the request came from and what it is for."""

    cookie: str
    site: str
    mode: str
    dest: str
    preload: bool  # (the worker asked the browser to start the page's request early, and this is that request)

    def line(self) -> str:
        """The facts on one line, for a log and for a report: `cookie=none site=none mode=navigate dest=document preload=yes`."""
        unsaid = "unsaid"
        return f"cookie={self.cookie} site={self.site or unsaid} mode={self.mode or unsaid} dest={self.dest or unsaid} preload={'yes' if self.preload else 'no'}"


def arrival(headers: Mapping[str, str], cookie: str | None) -> Arrival:
    """What the request with these (lower-case) `headers` carried; `cookie` is the sign-in cookie it had, if any."""
    return Arrival(
        cookie="refused" if cookie else "none",
        site=_word(headers.get("sec-fetch-site")),
        mode=_word(headers.get("sec-fetch-mode")),
        dest=_word(headers.get("sec-fetch-dest")),
        preload="service-worker-navigation-preload" in headers,
    )


HOW = {
    "none": "The page was opened by hand: typed, from a bookmark, or from the app's icon.",
    "same-origin": "The page was opened from a page of this app (a reload, or a link inside it).",
    "same-site": "The page was opened from another page of the same site.",
    "cross-site": "The page was opened from a page of another site.",
    "": "The browser did not say how the page was opened.",
    "other": "The browser said something unusual about how the page was opened.",
}


def why_note(arrived: Arrival) -> str:
    """The fold-out that says why the page asks: the cookie, and how the page was opened."""
    if arrived.cookie == "refused":
        cookie = "A sign-in cookie came with this request, but the server does not take it: the token was changed after this device signed in, or the cookie is from another server."
    else:
        cookie = "No sign-in cookie came with this request. This browser has none for this address (it never signed in here, or its data for this site was cleared), or it did not send the one it has."
    return (
        '<details class="note"><summary>Why does it ask for the token?</summary>'
        f"<p>{escape(cookie)}</p><p>{escape(HOW.get(arrived.site, HOW['other']))}</p>"
        f"<p>For a report: <code>{escape(arrived.line())}</code></p></details>"
    )


RESTORE = (
    '<p class="note" id="restore-note" role="status" hidden></p>'
    '<script src="/static/js/restore.js" defer></script>'
)


def tailnet_note(login: str | None, *, on: bool, allowed: bool) -> str:
    """What to tell a device that Tailscale has named (`login`): that it could be signed in without the token, or why it is not. `on`: the server
    signs in some Tailscale accounts; `allowed`: this one is among them (then the only reason it is not signed in is that its request
    came from a page of another site). Nothing when Tailscale named no one. The login is the viewer's own, and is shown as text."""
    if login is None:
        return ""
    who = f"<strong>{escape(login)}</strong>"
    if on and allowed:
        return (
            f'<p class="note">Tailscale says this device belongs to {who}, which this server signs in without the token, but this page was opened '
            "from another site. Open the server's address itself (type it, or use the app's icon) and you are signed in.</p>"
        )
    if on:
        return (
            f'<p class="note">Tailscale says this device belongs to {who}. This server does not sign in that account. To sign it in without the '
            "token, add it to <code>tailscale_logins</code> in the <code>[server]</code> section of config.toml on the server, and restart it.</p>"
        )
    line = f"tailscale_logins = [{json.dumps(login, ensure_ascii=False)}]"
    return (
        f'<p class="note">This server can sign this device in through Tailscale, with no token. Tailscale says it belongs to {who}. To allow that, '
        f"add this line to the <code>[server]</code> section of config.toml on the server, then restart it:</p><pre>{escape(line)}</pre>"
    )


def sign_in_page(
    *, problem: str = "", login_days: int = 0, tailscale_login: str | None = None, tailscale_on: bool = False, tailscale_allowed: bool = False,
    arrived: Arrival | None = None,
) -> str:
    """The page with the box for the token. `problem` says what went wrong with the last try (nothing on the first visit). The `tailscale_`
    arguments say what Tailscale told the server about the device (see `tailnet_note`). `arrived`, for the first visit, is what the request carried (it is
    explained in a fold-out). Where devices are kept signed in (`login_days`), the page's script tries the key that the app saved before anyone has to type."""
    stays = f" The device then stays signed in for {login_days} day{'s' if login_days != 1 else ''}." if login_days > 0 else ""
    return PAGE.format(
        problem=f'<p class="problem" role="alert">{escape(problem)}</p>' if problem else "",
        restore=RESTORE if login_days > 0 else "",
        tailnet=tailnet_note(tailscale_login, on=tailscale_on, allowed=tailscale_allowed),
        where="On the computer that runs the server, <code>mtk token</code> shows the token." + stays,
        why=why_note(arrived) if arrived is not None and not problem else "",
    )
