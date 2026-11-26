"""Who may ask. Subsonic puts the credentials in every request's query: `u` and `p` (or `p=enc:` and the password in hex), or `u`, `t` and `s` (`t` is
the MD5 of the password and the salt `s`, so that the password itself is not sent), or, in OpenSubsonic, an `apiKey` alone.

The password is the server's token (`mtk token`); the user name is whatever the client has (this server has no users). A device that Tailscale vouches for
(see tailnet.py) needs none of it. A wrong password is counted by the same limit as a wrong token on the sign-in page, so that guessing is as slow here.
"""

from __future__ import annotations

import hashlib

from musictoolkit.web.signin import GuessLimiter, same_token
from musictoolkit.web.subsonic.errors import (
    CLIENT_MUST_UPGRADE,
    CONFLICTING_MECHANISMS,
    GENERIC,
    INVALID_API_KEY,
    SERVER_MUST_UPGRADE,
    WRONG_CREDENTIALS,
    SubsonicError,
    missing,
)
from musictoolkit.web.subsonic.params import Params
from musictoolkit.web.subsonic.render import API_VERSION

SERVER_MAJOR, SERVER_MINOR = (int(part) for part in API_VERSION.split(".")[:2])


def check_version(params: Params) -> None:
    """`v` is the version of the API the client speaks, `c` its name: the two every call has to say. A client that speaks another major
    version, or a newer minor one, is told to upgrade whichever of the two has to."""
    version = params.require("v")
    params.require("c")
    parts = version.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        raise SubsonicError(GENERIC, f"{version!r} is not a version of the API") from None
    if major < SERVER_MAJOR:
        raise SubsonicError(CLIENT_MUST_UPGRADE, "Incompatible Subsonic REST protocol version. Client must upgrade.")
    if major > SERVER_MAJOR or minor > SERVER_MINOR:
        raise SubsonicError(SERVER_MUST_UPGRADE, "Incompatible Subsonic REST protocol version. Server must upgrade.")


def _password(given: str) -> str | None:
    """`p` is the password, or, after `enc:`, the bytes of it in hex. None for hex that is not."""
    if not given.startswith("enc:"):
        return given
    try:
        return bytes.fromhex(given[4:]).decode()
    except ValueError:
        return None


def _wrong(limiter: GuessLimiter, message: str = "Wrong username or password") -> SubsonicError:
    limiter.wrong()
    return SubsonicError(WRONG_CREDENTIALS, message)


def authenticate(params: Params, token: str, limiter: GuessLimiter) -> str | None:
    """Check the credentials against the server's token; the name the client gave (None for an API key, which has none) when they are right. Raises SubsonicError."""
    user, password, hashed, salt, key = (params.get(name) for name in ("u", "p", "t", "s", "apiKey"))
    offered = [value for value in (key, password, hashed) if value is not None]  # (an API key, a password, a digest: one of them at most)
    if len(offered) > 1 or (key is not None and user is not None):
        raise SubsonicError(CONFLICTING_MECHANISMS, "Multiple conflicting authentication mechanisms provided")
    if not offered:
        raise missing("u and p, or u, t and s, or apiKey")
    if pause := limiter.wait():
        raise SubsonicError(WRONG_CREDENTIALS, f"Too many wrong passwords. Try again in {pause} seconds.")
    if key is not None:
        if same_token(key, token):
            return None
        limiter.wrong()
        raise SubsonicError(INVALID_API_KEY, "Invalid API key")
    if user is None:
        raise missing("u")
    if password is not None:
        given = _password(password)
        if given is not None and same_token(given, token):
            return user
        raise _wrong(limiter)
    if salt is None:
        raise missing("s")
    expected = hashlib.md5((token + salt).encode(), usedforsecurity=False).hexdigest()  # (what the protocol asks for, not a secret kept by it)
    if same_token(hashed.lower(), expected):  # (a digest is what is left when there is no key and no password)
        return user
    raise _wrong(limiter)
