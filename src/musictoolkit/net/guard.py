"""Fetching addresses that came from outside, without letting them reach this computer's own network.

A radio station's address comes from a public directory, a podcast's from a feed, and either can be typed or pasted in. Any of them could
name something that is not on the internet at all: this computer itself (127.0.0.1), the router, a printer, a cloud's metadata service
(169.254.169.254). The app would then fetch it on someone else's behalf and hand the answer to the page.

So every connection made here is checked at the moment it is made. The name is looked up, every address it gives is examined, and only
ordinary public ones are connected to - and the connection goes to the very address that was checked, so a name whose answer changes
between two lookups (DNS rebinding) gains nothing. The check is on the connection, not on the text of the address, so redirects and odd
spellings of an address (http://2130706433/, http://[::ffff:127.0.0.1]/, a name that points at 10.0.0.1) all meet it.

Proxies from the environment are ignored (the check could not see where a proxy goes), cookies are not kept, and nothing about the
person is sent: a plain User-Agent and the headers the caller asks for.

For tests and the packaged-app check, MTK_ALLOW_LOCAL_URLS=1 (or `allow_local(True)`) lets addresses on this computer through; schemes, ports
and userinfo are still checked.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError, DecodeError, HTTPError, NameResolutionError, NewConnectionError, ProtocolError, ReadTimeoutError
from urllib3.util import connection as urllib3_connection

from musictoolkit import __version__

USER_AGENT = f"Music Toolkit/{__version__} (https://github.com/CBlasingameLLC/custom-music-application)"
MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 10  # a podcast episode's address often goes through several tracking services before it reaches the file
CONNECT_TIMEOUT = 8.0
READ_TIMEOUT = 20.0
REDIRECT_CODES = {301, 302, 303, 307, 308}

_allow_local = False


class UnsafeURL(ValueError):
    """The address cannot be used: not http or https, no host, credentials in it, or too long."""


class BlockedAddress(OSError):
    """The address names (or resolves to) a place that is not on the public internet."""


def allow_local(on: bool) -> None:
    """Let addresses on this computer through (tests only)."""
    global _allow_local
    _allow_local = bool(on)


def local_allowed() -> bool:
    return _allow_local or os.environ.get("MTK_ALLOW_LOCAL_URLS") == "1"


# ---------------------------------------------------------------- which addresses are public

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")


def is_public(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Is this an address on the public internet? Loopback, private, link-local, shared (CGNAT), multicast, reserved, documentation
    and the unspecified address are not. An IPv6 address that wraps an IPv4 one (mapped, NAT64) is as public as what it wraps; the
    tunnelling prefixes (6to4, Teredo) and everything outside IPv6's global range are never accepted."""
    try:
        ip = ipaddress.ip_address(address) if isinstance(address, str) else address
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public(ip.ipv4_mapped)
        if ip in _NAT64:
            return is_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip not in _GLOBAL_UNICAST_V6 or ip.sixtofour is not None or ip.teredo is not None or ip.scope_id:
            return False
    return bool(
        ip.is_global
        and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)
    )


def check_url(url: str) -> str:
    """The address, if it is one this module may fetch (otherwise UnsafeURL). A local address is only caught when the connection is made."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURL("There is no address.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURL("The address is too long.")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        raise UnsafeURL("The address has a space or a control character in it.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("The address is not valid.") from exc
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http:// and https:// addresses can be used.")
    if not parts.hostname:
        raise UnsafeURL("The address has no host name.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("An address with a user name or password in it cannot be used.")
    if port is not None and not 0 < port < 65536:
        raise UnsafeURL("The port is not valid.")
    return url


# ---------------------------------------------------------------- the connection that checks

# A host that is nothing but numbers written the way old software accepted them: 2130706433, 0x7f.0.0.1, 017700000001, 127.1, 010.1.1.1. The system's own
# lookup reads some of these as addresses (glibc: every one) and refuses others (Windows), so the answer would depend on the operating system; they are
# refused here, the same everywhere. No real host name ends in a number.
_UNUSUAL_NUMBER = re.compile(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}", re.IGNORECASE)


def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` has, in the order the system gives them; BlockedAddress if any is not public (unless local ones are allowed)."""
    host = host.strip("[]")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None  # a name
    if literal is None and _UNUSUAL_NUMBER.fullmatch(host):
        raise BlockedAddress(f"{host} is a number written in an unusual way; this app only connects to addresses written the usual way (such as 93.184.216.34).")
    if literal is not None and not local_allowed() and not is_public(literal):
        raise BlockedAddress(f"{host} is on this computer or its local network, not on the internet.")
    try:
        infos = socket.getaddrinfo(host, port, urllib3_connection.allowed_gai_family(), socket.SOCK_STREAM)
    except UnicodeError as exc:
        raise socket.gaierror(f"{host!r} is not a name that can be looked up") from exc
    addresses: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = sockaddr[0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise socket.gaierror(f"{host!r} has no address")
    if not local_allowed():
        blocked = [address for address in addresses if not is_public(address)]
        if blocked:
            raise BlockedAddress(f"{host} is on this computer or its local network ({blocked[0]}), not on the internet.")
    return addresses


class _GuardedMixin:
    """Connect to an address that has been checked, instead of letting the library look the name up again."""

    def _new_conn(self) -> socket.socket:  # urllib3's own hook for opening the socket
        try:
            last: OSError | None = None
            for address in _resolve(self._dns_host, self.port):  # type: ignore[attr-defined]
                try:
                    return urllib3_connection.create_connection(
                        (address, self.port), self.timeout, source_address=self.source_address, socket_options=self.socket_options,  # type: ignore[attr-defined]
                    )
                except OSError as exc:
                    last = exc
            raise last or OSError("no address to connect to")
        except BlockedAddress:
            raise
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc  # type: ignore[attr-defined]
        except socket.timeout as exc:
            raise ConnectTimeoutError(self, f"Connection to {self.host} timed out. (connect timeout={self.timeout})") from exc  # type: ignore[attr-defined]
        except OSError as exc:
            raise NewConnectionError(self, f"Failed to establish a new connection: {exc}") from exc  # type: ignore[arg-type]


class _IcyAwareResponse(http.client.HTTPResponse):
    """SHOUTcast version 1 servers begin their answer with "ICY 200 OK" instead of "HTTP/1.0 200 OK"; the standard library refuses that."""

    def _read_status(self):  # type: ignore[no-untyped-def]
        line = str(self.fp.readline(65537), "iso-8859-1")
        if len(line) > 65536:
            raise http.client.LineTooLong("status line")
        if not line:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        try:
            version, status, reason = line.split(None, 2)
        except ValueError:
            try:
                version, status = line.split(None, 1)
                reason = ""
            except ValueError:
                version = ""
        if version == "ICY":
            version = "HTTP/1.0"
        if not version.startswith("HTTP/"):
            self._close_conn()
            raise http.client.BadStatusLine(line)
        try:
            status = int(status)
        except ValueError:
            raise http.client.BadStatusLine(line) from None
        if status < 100 or status > 999:
            raise http.client.BadStatusLine(line)
        return version, status, reason


class _GuardedHTTPConnection(_GuardedMixin, HTTPConnection):
    response_class = _IcyAwareResponse


class _GuardedHTTPSConnection(_GuardedMixin, HTTPSConnection):
    response_class = _IcyAwareResponse


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _GuardedHTTPPool, "https": _GuardedHTTPSPool}


def new_session() -> requests.Session:
    """A session that only connects to public addresses, ignores proxies in the environment and keeps no cookies between requests."""
    session = requests.Session()
    session.trust_env = False
    adapter = _GuardedAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers["User-Agent"] = USER_AGENT
    return session


# ---------------------------------------------------------------- fetching

class FetchError(Exception):
    """An address could not be fetched; the message says why in words a person can read."""


def _explain(exc: Exception) -> str:
    """What went wrong, from a requests exception (BlockedAddress and the like are found in its chain)."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BlockedAddress):
            return str(current)
        if isinstance(current, UnsafeURL):
            return str(current)
        stack.extend([current.__cause__, current.__context__, getattr(current, "reason", None)])
        for arg in getattr(current, "args", ()):
            if isinstance(arg, BaseException):
                stack.append(arg)
    if isinstance(exc, (requests.Timeout, ReadTimeoutError, socket.timeout)):
        return "The server took too long to answer."
    if isinstance(exc, (ProtocolError, http.client.HTTPException)):
        return "The connection was cut off."
    if isinstance(exc, (DecodeError, zlib.error)):
        return "The answer could not be unpacked."
    if isinstance(exc, requests.exceptions.SSLError):
        return "The server's security certificate could not be trusted."
    if isinstance(exc, requests.ConnectionError):
        return "Could not connect to the server."
    return f"The request failed ({type(exc).__name__})."


def open_stream(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    connect_timeout: float = CONNECT_TIMEOUT,
    read_timeout: float = READ_TIMEOUT,
    max_redirects: int = MAX_REDIRECTS,
    session: requests.Session | None = None,
) -> tuple[requests.Response, str]:
    """GET `url` and return the response (body not yet read; close it when done) and the address it ended up at.

    Redirects are followed by hand so that every hop is checked like the first. Anything but a final answer raises FetchError."""
    own = session is None
    client = session or new_session()
    current = check_url(url)
    try:
        for _hop in range(max_redirects + 1):
            try:
                response = client.get(current, headers=headers or {}, stream=True, allow_redirects=False, timeout=(connect_timeout, read_timeout))
            except UnsafeURL as exc:
                raise FetchError(str(exc)) from exc
            except requests.RequestException as exc:
                raise FetchError(_explain(exc)) from exc
            if response.status_code in REDIRECT_CODES and response.headers.get("Location"):
                response.close()
                try:
                    current = check_url(urljoin(current, response.headers["Location"]))
                except UnsafeURL as exc:
                    raise FetchError(f"The address sent us on to one that cannot be used: {exc}") from exc
                continue
            return response, current
        raise FetchError("The address sent us round too many times.")
    except BaseException:
        if own:
            client.close()
        raise


@dataclass
class Fetched:
    content: bytes
    url: str
    status: int
    headers: dict[str, str]


def fetch(
    url: str,
    *,
    max_bytes: int,
    deadline: float = 30.0,
    headers: dict[str, str] | None = None,
    accepted_status: tuple[int, ...] = (200,),
) -> Fetched:
    """The whole body of `url`, never more than `max_bytes` (after decoding) and never taking longer than `deadline` seconds."""
    started = time.monotonic()
    with new_session() as session:
        response, final = open_stream(url, headers=headers, session=session)
        with response:
            if response.status_code not in accepted_status:
                raise FetchError(f"The server answered with error {response.status_code}.")
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes and "content-encoding" not in response.headers:
                raise FetchError("The answer is larger than this app will read.")
            pieces: list[bytes] = []
            size = 0
            try:
                for piece in body_pieces(response, 65536):
                    size += len(piece)
                    if size > max_bytes:
                        raise FetchError("The answer is larger than this app will read.")
                    if time.monotonic() - started > deadline:
                        raise FetchError("The server was too slow to answer.")
                    pieces.append(piece)
            except (requests.RequestException, HTTPError, http.client.HTTPException, OSError, zlib.error) as exc:
                raise FetchError(_explain(exc)) from exc
            return Fetched(b"".join(pieces), final, response.status_code, {k.lower(): v for k, v in response.headers.items()})


def body_pieces(response: requests.Response, size: int = 16384) -> Iterator[bytes]:
    """The body of a streamed response as it arrives: each piece is whatever the connection had ready, at most `size` bytes, unpacked.
    (iter_content waits until it has a whole chunk, which lets a server that trickles hold the reader for as long as it likes.)"""
    while True:
        piece = response.raw.read1(size, decode_content=True)
        if not piece:
            return
        yield piece


def iter_body(response: requests.Response, size: int = 16384) -> Iterator[bytes]:
    """`body_pieces` for a server of our own: a failure part-way ends the stream instead of raising into it. Closes the response."""
    try:
        yield from body_pieces(response, size)
    except (requests.RequestException, HTTPError, http.client.HTTPException, OSError, zlib.error):
        return
    finally:
        response.close()


@contextmanager
def local_urls_allowed() -> Iterator[None]:
    """`with local_urls_allowed():` for tests."""
    before = _allow_local
    allow_local(True)
    try:
        yield
    finally:
        allow_local(before)
