from pathlib import Path

from tests.conftest import build_web


def test_fresh_install_serves_every_page_with_no_data(empty_web) -> None:
    client = empty_web.client
    assert client.get("/").status_code == 200
    for url in ("/api/about", "/api/tracks", "/api/albums", "/api/artists", "/api/facets", "/api/home",
                "/api/playlists", "/api/settings", "/api/recommendations", "/api/history/top-artists",
                "/api/devices", "/api/jobs"):
        assert client.get(url).status_code == 200, url
    assert client.get("/api/tracks").json() == {"total": 0, "items": []}


def test_about_reports_version_and_paths(web) -> None:
    about = web.client.get("/api/about").json()
    assert about["tracks"] == 9
    assert about["version"].count(".") == 2
    assert Path(about["db_path"]).name == "library.db"


def test_unhandled_errors_are_json_with_a_message(web, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("musictoolkit.web.routers.library.list_tracks", boom)
    response = web.client.get("/api/tracks")
    assert response.status_code == 500
    assert response.json()["detail"] == "RuntimeError: simulated failure"


def test_index_and_static_assets_are_served(empty_web) -> None:
    index = empty_web.client.get("/")
    assert "text/html" in index.headers["content-type"]
    assert index.headers["content-security-policy"].startswith("default-src 'self'")
    assert index.headers["x-content-type-options"] == "nosniff"


class TestAuth:
    def test_api_needs_the_launch_token(self, tmp_path) -> None:
        web = build_web(tmp_path, None, token="s3cret")
        assert web.client.get("/api/about").status_code == 401
        assert web.client.get("/api/about", headers={"x-mtk-token": "wrong"}).status_code == 401
        assert web.client.get("/api/about", headers={"x-mtk-token": "s3cret"}).status_code == 200

    def test_opening_with_the_token_sets_a_strict_cookie_that_unlocks_the_api(self, tmp_path) -> None:
        web = build_web(tmp_path, None, token="s3cret")
        assert web.client.get("/").status_code == 401  # no token: a page that explains how to open the app
        opened = web.client.get("/", params={"token": "s3cret"})
        assert opened.status_code == 200
        cookie = opened.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert web.client.get("/api/about").status_code == 200  # the cookie rides along

    def test_a_wrong_token_does_not_open_the_app(self, tmp_path) -> None:
        web = build_web(tmp_path, None, token="s3cret")
        assert web.client.get("/", params={"token": "nope"}).status_code == 401

    def test_streams_and_art_need_auth_too(self, tmp_path, music_dir) -> None:
        from tests.conftest import scan

        web = build_web(tmp_path, music_dir, token="s3cret")
        web.client.headers["x-mtk-token"] = "s3cret"
        scan(web)
        track_id = web.client.get("/api/tracks").json()["items"][0]["id"]
        del web.client.headers["x-mtk-token"]
        assert web.client.get(f"/api/tracks/{track_id}/stream").status_code == 401
        assert web.client.get(f"/api/art/track/{track_id}").status_code == 401


class TestHostHeader:
    def test_non_loopback_host_is_refused(self, empty_web) -> None:
        """DNS rebinding: a page on evil.example resolving to 127.0.0.1 must not reach the API."""
        response = empty_web.client.get("/api/about", headers={"host": "evil.example:4533"})
        assert response.status_code == 403

    def test_loopback_hosts_are_allowed(self, empty_web) -> None:
        for host in ("127.0.0.1:4533", "localhost:4533", "[::1]:4533"):
            assert empty_web.client.get("/api/about", headers={"host": host}).status_code == 200, host
