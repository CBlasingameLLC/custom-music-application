"""Which Host header names the app answers to.

A page on another site can make a browser send requests to the app's address with its own name in the Host header (DNS
rebinding). The app therefore answers only to the names it is reached by: the loopback ones always, and, for a server, the
exact names the person listed (the machine's local name, the name on the private network). Nothing is matched by pattern.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "testserver"})

_NAME = re.compile(r"^[a-z0-9_]([a-z0-9_.-]*[a-z0-9_])?$")
_IPV6 = re.compile(r"^\[[0-9a-f:.]+\]$")
_IPV6_BARE = re.compile(r"^[0-9a-f:.]+$")


def hostname_of(header: str) -> str:
    """The Host header without its port, lower case: '127.0.0.1:4533' -> '127.0.0.1', '[::1]:4533' -> '[::1]'."""
    header = header.strip().lower()
    if header.startswith("["):
        return header.split("]")[0] + "]"
    return header.rsplit(":", 1)[0] if ":" in header else header


def clean_names(names: Iterable[str]) -> list[str]:
    """The names as the guard compares them (lower case, an IPv6 address in brackets), each once, in the order given.
    Raises ValueError, naming the entry, for anything that is not a plain host name or address: a wildcard, an address with
    a scheme or a path, a name with a port, an empty entry."""
    out: list[str] = []
    for raw in names:
        text = str(raw).strip().lower()
        if not text:
            raise ValueError("an allowed host is empty")
        if "://" in text or "/" in text:
            raise ValueError(f"{raw!r}: write the name only, not an address")
        if "*" in text:
            raise ValueError(f"{raw!r}: wildcards are not accepted; list each name")
        if text.startswith("["):
            if not _IPV6.match(text):
                raise ValueError(f"{raw!r}: write an IPv6 address in brackets, without a port")
            name = text
        elif text.count(":") >= 2:
            if not _IPV6_BARE.match(text):
                raise ValueError(f"{raw!r} is not a host name or address")
            name = f"[{text}]"
        elif ":" in text:
            raise ValueError(f"{raw!r}: write the name without a port")
        elif _NAME.match(text):
            name = text
        else:
            raise ValueError(f"{raw!r} is not a host name or address")
        if name not in out:
            out.append(name)
    return out
