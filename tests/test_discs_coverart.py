"""The front cover from the Cover Art Archive, stood in for by a server on this computer."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from musictoolkit.discs import coverart
from musictoolkit.net import guard
from tests.fake_musicbrainz import coverart_script, ident
from tests.fake_streams import png_bytes, serve_script

RELEASE = ident("release-a")
GROUP = ident("group-a")


@pytest.fixture(autouse=True)
def stand_in_allowed():
    with guard.local_urls_allowed():
        yield


def jpeg_of(data: bytes | None) -> Image.Image:
    assert data is not None
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG"
    return image


def test_the_cover_is_fetched_through_the_redirect_and_handed_back_as_a_jpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(coverart_script({f"release/{RELEASE}": png_bytes((600, 400), (10, 120, 200))})) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        image = jpeg_of(coverart.front_cover(RELEASE, GROUP))
    assert image.size == (600, 400) and image.getpixel((300, 200))[2] > 150
    assert [r["path"] for r in server.requests] == [f"/release/{RELEASE}/front-1200", f"/image/release/{RELEASE}"]


def test_a_big_picture_is_made_smaller_and_a_transparent_one_is_laid_on_white(monkeypatch: pytest.MonkeyPatch) -> None:
    clear = io.BytesIO()
    Image.new("RGBA", (2800, 2000), (0, 0, 0, 0)).save(clear, "PNG")
    with serve_script(coverart_script({f"release/{RELEASE}": clear.getvalue()})) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        image = jpeg_of(coverart.front_cover(RELEASE))
    assert max(image.size) == coverart.MAX_SIDE and image.size == (1400, 1000)
    assert min(image.getpixel((5, 5))) > 250


def test_an_album_without_its_own_cover_gets_the_one_shown_for_the_release_group(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(coverart_script({f"release-group/{GROUP}": png_bytes((300, 300))})) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        jpeg_of(coverart.front_cover(RELEASE, GROUP))
    assert server.requests[0]["path"] == f"/release/{RELEASE}/front-1200"
    assert any(r["path"] == f"/image/release-group/{GROUP}" for r in server.requests)


def test_no_cover_anywhere_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(coverart_script({})) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        assert coverart.front_cover(RELEASE, GROUP) is None


@pytest.mark.parametrize("payload", [b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", b"<html>not a picture</html>", b"", b"\x89PNG not really"])
def test_whatever_is_not_a_picture_is_not_passed_on(payload: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(coverart_script({f"release/{RELEASE}": payload}, content_type="image/jpeg")) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        assert coverart.front_cover(RELEASE) is None


def test_a_picture_with_too_many_pixels_is_refused_before_it_is_drawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coverart, "MAX_PIXELS", 100)
    with serve_script(coverart_script({f"release/{RELEASE}": png_bytes((20, 20))})) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        assert coverart.front_cover(RELEASE) is None


def test_an_id_that_does_not_look_like_an_id_is_never_put_in_an_address(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(coverart_script({})) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        for bad in ("../../etc/passwd", "x?y=1", f"{RELEASE}/../x", "", None, RELEASE.upper()):
            assert coverart.front_cover(bad, bad) is None
    assert server.requests == []


def test_an_address_on_this_computer_is_refused_unless_a_test_allows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(coverart_script({f"release/{RELEASE}": png_bytes((10, 10))})) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        guard.allow_local(False)
        try:
            assert coverart.front_cover(RELEASE) is None
        finally:
            guard.allow_local(True)
    assert server.requests == []


def test_a_picture_that_is_too_big_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    big = io.BytesIO()
    Image.frombytes("RGB", (2000, 1500), os.urandom(2000 * 1500 * 3)).save(big, "PNG")  # a perfectly good picture of more than 8 MB
    assert len(big.getvalue()) > coverart.MAX_BYTES
    with serve_script(coverart_script({f"release/{RELEASE}": big.getvalue()})) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        assert coverart.front_cover(RELEASE) is None


@pytest.mark.parametrize("kind", ["BMP", "TIFF"])
def test_only_pictures_of_the_usual_kinds_are_taken(kind: str) -> None:
    out = io.BytesIO()
    Image.new("RGB", (50, 50), (1, 2, 3)).save(out, kind)
    assert coverart.as_jpeg(out.getvalue()) is None
    plain = io.BytesIO()
    Image.new("RGB", (50, 50), (1, 2, 3)).save(plain, "PNG")
    assert coverart.as_jpeg(plain.getvalue()) is not None
