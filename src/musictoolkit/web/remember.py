"""The key that gets a device its sign-in back when its browser has lost the cookie.

A device that signed in is meant to stay signed in for `login_days`, and the cookie it was given says so. But a web app on a Mac's Dock or a phone's Home
Screen is a browser of its own, with storage of its own, and there are reports (WebKit's bug tracker has them) of such apps starting without the cookies they were
closed with. Such a device would ask for the token every time it was opened.

So the app also keeps a key in the page's own storage (`localStorage`), and the page that asks for the token tries it before it asks. The key is made from the
token, so it is good only while the token is the one it was made from (changing the token signs every device out, as it does for the cookie), and it carries the day
it ends, which the server checks (not the browser). It is not the token and it is no good as one: the server takes it at one address only, `/signin/restore`,
where it is exchanged for the same cookie that the token buys. Nothing is kept on the server: a key is checked by making it again.

    r1.<the second it ends, in Unix time>.<HMAC-SHA256 of that second, with the token as the secret, in base64url>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from musictoolkit.web.signin import same_token

PREFIX = "r1"
LABEL = b"music-toolkit remember key v1\x00"  # (so that a MAC made for this is never one made for anything else the token signs: see tailnet.py)
LONGEST = 400 * 86400  # seconds from now that a key may still have to run: a key that says more was made when the clock was wrong
MOST_CHARACTERS = 120  # (a key is 2 + 1 + 10 + 1 + 43 characters long)
_SECOND = re.compile(r"[0-9]{1,11}")


def make(token: str, expires: int) -> str:
    """The key for `token` that stops being good at second `expires` (Unix time)."""
    mac = hmac.new(token.encode("utf-8", "surrogatepass"), LABEL + str(int(expires)).encode("ascii"), hashlib.sha256).digest()
    return f"{PREFIX}.{int(expires)}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode('ascii')}"


def valid(key: str, token: str, now: float) -> bool:
    """Is `key` one that was made from `token`, and is it still good at second `now`? The MAC is compared in constant time (how far a wrong key matches is
    not told by how long the answer takes), and a key that is malformed, ended, or runs further than a key ever does is refused without a MAC being made."""
    if not isinstance(key, str) or len(key) > MOST_CHARACTERS:
        return False
    parts = key.split(".")
    if len(parts) != 3 or parts[0] != PREFIX or not _SECOND.fullmatch(parts[1]):
        return False
    expires = int(parts[1])
    if expires <= now or expires > now + LONGEST:
        return False
    return same_token(key, make(token, expires))
