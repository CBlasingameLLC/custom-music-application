"""net.guard: addresses that came from outside are fetched only if they lead to the public internet."""

from __future__ import annotations

import gzip
import socket
import time

import pytest

from musictoolkit.net import guard
from tests.fake_streams import serve_script


@pytest.fixture
def local():
    """Addresses on this computer are allowed, so the fake servers of these tests can be reached."""
    with guard.local_urls_allowed():
        yield


def _reply(h, body: bytes = b"", status: int = 200, **headers: str) -> None:
    h.send_response(status)
    for name, value in headers.items():
        h.send_header(name.replace("_", "-"), value)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


# ---------------------------------------------------------------- which addresses count as public

@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "2a00:1450:4001:81b::200e"])
def test_ordinary_internet_addresses_are_public(address) -> None:
    assert guard.is_public(address)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "127.255.255.254",  # this computer
        "10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.1",  # private networks
        "169.254.169.254", "169.254.0.1",  # link-local, the cloud metadata service
        "100.64.0.1", "100.127.255.255",  # shared address space (carrier-grade NAT)
        "0.0.0.0", "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255",  # unspecified, multicast, reserved, broadcast
        "192.0.2.1", "198.51.100.7", "203.0.113.9",  # documentation ranges
        "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1",  # IPv6 loopback, unspecified, link-local, unique-local, multicast
        "::ffff:127.0.0.1", "::ffff:10.1.2.3", "::ffff:192.168.0.9",  # IPv4 inside IPv6
        "2002:7f00:1::", "2002:c0a8:101::",  # 6to4 carrying 127.0.0.1 and 192.168.1.1
        "64:ff9b::7f00:1", "64:ff9b::a00:1",  # NAT64 carrying 127.0.0.1 and 10.0.0.1
        "not an address", "", "999.1.1.1",
    ],
)
def test_addresses_on_this_computer_or_its_network_are_not_public(address) -> None:
    assert not guard.is_public(address)


def test_an_ipv6_address_wrapping_a_public_ipv4_one_is_as_public_as_it_is() -> None:
    assert guard.is_public("::ffff:8.8.8.8") and guard.is_public("64:ff9b::808:808")


def test_the_tunnelling_prefixes_are_never_accepted_even_around_a_public_address() -> None:
    assert not guard.is_public("2002:808:808::") and not guard.is_public("2001:0:4136:e378:8000:63bf:3fff:fdd2")


# ---------------------------------------------------------------- which addresses may be asked for at all

@pytest.mark.parametrize(
    "url", ["http://example.com/stream", "https://example.com:8443/a?b=c#d", "http://203.0.113.5:8000/", "https://[2606:4700:4700::1111]/x"]
)
def test_web_addresses_pass_the_text_check(url) -> None:
    assert guard.check_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "", "   ", None, 12,
        "ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "data:audio/mpeg;base64,AAAA", "gopher://example.com/",
        "//example.com/x", "example.com/stream", "http://", "http:///path",
        "http://user:secret@example.com/", "http://user@example.com/",
        "http://example.com:0/", "http://example.com:99999/", "http://example.com:abc/",
        "http://exa mple.com/", "http://example.com/a\nb", "http://example.com/\x00",
        "http://example.com/" + "a" * 3000,
    ],
)
def test_other_addresses_are_refused_by_the_text_check(url) -> None:
    with pytest.raises(guard.UnsafeURL):
        guard.check_url(url)


# ---------------------------------------------------------------- the check at the moment of connecting

def _answers(monkeypatch, *rounds: list[str]) -> list[str]:
    """Make name lookups answer with these addresses, one list per lookup (the last list repeats). Returns the names asked about."""
    asked: list[str] = []
    remaining = list(rounds)

    def fake(host, port, family=0, type=0, *args):
        asked.append(host)
        answer = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answer]

    monkeypatch.setattr(guard.socket, "getaddrinfo", fake)
    return asked


def test_a_name_with_only_public_addresses_resolves(monkeypatch) -> None:
    _answers(monkeypatch, ["93.184.216.34", "2606:2800:220:1::1"])
    assert guard._resolve("radio.example", 80) == ["93.184.216.34", "2606:2800:220:1::1"]


@pytest.mark.parametrize("private", ["127.0.0.1", "10.1.2.3", "192.168.1.5", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_a_name_that_points_inside_is_refused(monkeypatch, private) -> None:
    _answers(monkeypatch, [private])
    with pytest.raises(guard.BlockedAddress, match="not on the internet"):
        guard._resolve("sneaky.example", 80)


def test_one_private_address_among_public_ones_is_enough_to_refuse_the_name(monkeypatch) -> None:
    _answers(monkeypatch, ["93.184.216.34", "10.0.0.7"])
    with pytest.raises(guard.BlockedAddress):
        guard._resolve("mixed.example", 80)


def test_a_literal_address_is_checked_like_a_name() -> None:
    for literal in ("127.0.0.1", "[::1]", "[fe80::1]", "[::ffff:10.0.0.1]", "2130706433", "0x7f.0.0.1", "017700000001", "127.1", "10.1.1.1", "localhost"):
        with pytest.raises(guard.BlockedAddress):
            guard._resolve(literal, 80)


def test_local_addresses_pass_only_when_they_have_been_allowed(local) -> None:
    assert guard._resolve("127.0.0.1", 80) == ["127.0.0.1"]


def test_the_connection_goes_to_the_address_that_was_checked_and_the_name_is_not_looked_up_again(monkeypatch) -> None:
    """DNS rebinding: a name that answers with a public address for the check and a private one for the connection gets nowhere."""
    asked = _answers(monkeypatch, ["93.184.216.34"], ["127.0.0.1"])
    connected: list[tuple] = []

    def connect(address, timeout, source_address=None, socket_options=None):
        connected.append(address)
        return "a socket"

    monkeypatch.setattr(guard.urllib3_connection, "create_connection", connect)
    connection = guard._GuardedHTTPConnection("rebind.example", 80, timeout=3)
    assert connection._new_conn() == "a socket"
    assert connected == [("93.184.216.34", 80)] and asked == ["rebind.example"], "connected to the checked address, with no second lookup"
    # the next connection looks the name up again, is told 127.0.0.1 this time, and is refused
    with pytest.raises(guard.BlockedAddress):
        connection._new_conn()
    assert connected == [("93.184.216.34", 80)]


def test_the_next_address_is_tried_when_the_first_does_not_answer(monkeypatch) -> None:
    _answers(monkeypatch, ["93.184.216.34", "2606:2800:220:1::1"])
    tried: list[str] = []

    def connect(address, timeout, source_address=None, socket_options=None):
        tried.append(address[0])
        if address[0] == "93.184.216.34":
            raise ConnectionRefusedError("no")
        return "a socket"

    monkeypatch.setattr(guard.urllib3_connection, "create_connection", connect)
    assert guard._GuardedHTTPSConnection("multi.example", 443, timeout=3)._new_conn() == "a socket"
    assert tried == ["93.184.216.34", "2606:2800:220:1::1"]


def test_a_real_server_on_this_computer_is_never_connected_to() -> None:
    with serve_script(lambda h: _reply(h, b"secret")) as server:
        for url in (server.url + "/", f"http://localhost:{server.port}/", f"http://[::ffff:127.0.0.1]:{server.port}/"):
            with pytest.raises(guard.FetchError, match="on this computer"):
                guard.fetch(url, max_bytes=1000)
        assert server.requests == [], "not one request reached it"


# ---------------------------------------------------------------- fetching (local servers allowed)

def test_fetch_returns_the_body_and_what_the_server_said(local) -> None:
    with serve_script(lambda h: _reply(h, b'{"ok": true}', Content_Type="application/json", ETag='"v1"')) as server:
        got = guard.fetch(server.url + "/feed", max_bytes=1000, headers={"X-Test": "yes"})
    assert got.content == b'{"ok": true}' and got.status == 200 and got.headers["etag"] == '"v1"'
    assert server.requests[0]["headers"]["X-Test"] == "yes"
    assert server.requests[0]["headers"]["User-Agent"].startswith("Music Toolkit/")


def test_no_cookies_proxy_settings_or_credentials_go_along(local, monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    with serve_script(lambda h: _reply(h, b"x", Set_Cookie="a=b")) as server:
        guard.fetch(server.url + "/one", max_bytes=10)
        guard.fetch(server.url + "/two", max_bytes=10)
    assert "Cookie" not in server.requests[1]["headers"] and "Authorization" not in server.requests[1]["headers"]


def test_redirects_are_followed_and_the_final_address_is_reported(local) -> None:
    def script(h):
        if h.path == "/a":
            _reply(h, b"", 302, Location="/b")
        elif h.path == "/b":
            _reply(h, b"", 301, Location=f"http://127.0.0.1:{h.server.server_address[1]}/c")
        else:
            _reply(h, b"arrived")

    with serve_script(script) as server:
        got = guard.fetch(server.url + "/a", max_bytes=100)
    assert got.content == b"arrived" and got.url == server.url + "/c"


def test_a_redirect_to_an_address_that_cannot_be_used_is_refused(local) -> None:
    for target in ("ftp://example.com/x", "file:///etc/passwd", "http://user:pw@example.com/"):
        with serve_script(lambda h, target=target: _reply(h, b"", 302, Location=target)) as server:
            with pytest.raises(guard.FetchError, match="cannot be used"):
                guard.fetch(server.url + "/", max_bytes=100)


def test_endless_redirects_are_cut_off(local) -> None:
    with serve_script(lambda h: _reply(h, b"", 302, Location="/again")) as server:
        with pytest.raises(guard.FetchError, match="too many times"):
            guard.fetch(server.url + "/", max_bytes=100)
    assert len(server.requests) == guard.MAX_REDIRECTS + 1


def test_a_redirect_to_a_place_that_is_not_public_is_refused_at_the_second_hop(monkeypatch) -> None:
    """The first server counts as public for this test (it is the only one); it sends the client to a second one, which is not."""
    try:
        second_host = "127.0.0.2"
        socket.socket().bind((second_host, 0))
    except OSError:
        pytest.skip("this system does not let a server listen on 127.0.0.2")
    monkeypatch.setattr(guard, "is_public", lambda address: str(address) == "127.0.0.1")
    with serve_script(lambda h: _reply(h, b"inside"), host=second_host) as inside:
        with serve_script(lambda h: _reply(h, b"", 302, Location=inside.url + "/")) as outside:
            with pytest.raises(guard.FetchError, match="127.0.0.2"):
                guard.fetch(outside.url + "/", max_bytes=100)
        assert inside.requests == [] and len(outside.requests) == 1


def test_an_answer_larger_than_allowed_is_refused_whether_or_not_it_says_so_in_advance(local) -> None:
    with serve_script(lambda h: _reply(h, b"x" * 5000)) as server:
        with pytest.raises(guard.FetchError, match="larger"):
            guard.fetch(server.url + "/", max_bytes=1000)

    def unannounced(h):  # no Content-Length: the body just goes on until the connection closes
        h.send_response(200)
        h.end_headers()
        for _ in range(100):
            h.wfile.write(b"y" * 100)

    with serve_script(unannounced) as server:
        with pytest.raises(guard.FetchError, match="larger"):
            guard.fetch(server.url + "/", max_bytes=1000)


def test_a_compressed_answer_is_limited_by_its_size_once_unpacked(local) -> None:
    squashed = gzip.compress(b"\0" * 5_000_000)
    assert len(squashed) < 10_000

    with serve_script(lambda h: _reply(h, squashed, Content_Encoding="gzip")) as server:
        with pytest.raises(guard.FetchError, match="larger"):
            guard.fetch(server.url + "/", max_bytes=100_000)


def test_a_server_that_trickles_is_given_up_on(local) -> None:
    def trickle(h):
        h.send_response(200)
        h.end_headers()
        for _ in range(50):
            h.wfile.write(b".")
            h.wfile.flush()
            time.sleep(0.05)

    with serve_script(trickle) as server:
        started = time.monotonic()
        with pytest.raises(guard.FetchError, match="too slow"):
            guard.fetch(server.url + "/", max_bytes=1000, deadline=0.3)
        assert time.monotonic() - started < 2


def test_an_error_status_is_reported_in_words(local) -> None:
    with serve_script(lambda h: _reply(h, b"nope", 404)) as server:
        with pytest.raises(guard.FetchError, match="error 404"):
            guard.fetch(server.url + "/", max_bytes=100)
    got = None
    with serve_script(lambda h: _reply(h, b"", 304)) as server:
        got = guard.fetch(server.url + "/", max_bytes=100, accepted_status=(200, 304))
    assert got.status == 304


def test_a_server_that_is_not_there_is_reported_in_words(local) -> None:
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    with pytest.raises(guard.FetchError, match="Could not connect"):
        guard.fetch(f"http://127.0.0.1:{port}/", max_bytes=100)
