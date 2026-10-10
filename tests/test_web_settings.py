import os
from pathlib import Path

from musictoolkit.config import load_config
from musictoolkit.integrations.listenbrainz_client import ListenBrainzError
from musictoolkit.web.routers import settings as settings_router
from tests.conftest import build_web, ids_by_title, make_track, scan


class TestSettings:
    def test_defaults_never_expose_secrets(self, empty_web) -> None:
        settings = empty_web.client.get("/api/settings").json()
        assert settings["listenbrainz"] == {"enabled": True, "username": "", "has_token": False, "scrobble": True, "now_playing": False}
        assert "user_token" not in settings["listenbrainz"] and "api_key" not in settings["lastfm"]

    def test_update_is_partial_persisted_and_masks_secrets(self, empty_web) -> None:
        c = empty_web.client
        response = c.put("/api/settings", json={
            "musicbrainz": {"contact": "me@example.com"},
            "listenbrainz": {"username": "cayl", "user_token": "tok-123"},
            "app": {"lyrics_lrclib": True},
        })
        assert response.status_code == 200
        body = response.json()
        assert body["musicbrainz"]["contact"] == "me@example.com"
        assert body["listenbrainz"]["has_token"] is True and "tok-123" not in response.text
        assert body["app"] == {"rescan_on_launch": True, "lyrics_lrclib": True, "auto_update": True}

        saved = load_config(empty_web.ctx.config_path)  # really written to config.toml
        assert saved.listenbrainz.user_token == "tok-123" and saved.listenbrainz.username == "cayl"
        # an untouched key keeps its value across a later partial update
        c.put("/api/settings", json={"listenbrainz": {"scrobble": False}})
        assert load_config(empty_web.ctx.config_path).listenbrainz.user_token == "tok-123"

    def test_automatic_updates_can_be_switched_off_and_stay_off(self, empty_web) -> None:
        c = empty_web.client
        assert c.get("/api/settings").json()["app"]["auto_update"] is True
        assert c.put("/api/settings", json={"app": {"auto_update": False}}).json()["app"]["auto_update"] is False
        assert load_config(empty_web.ctx.config_path).app.auto_update is False
        assert c.put("/api/settings", json={"app": {"auto_update": "no"}}).status_code == 422

    def test_sending_an_empty_secret_clears_it(self, empty_web) -> None:
        c = empty_web.client
        c.put("/api/settings", json={"lastfm": {"api_key": "k", "api_secret": "s"}})
        cleared = c.put("/api/settings", json={"lastfm": {"api_key": ""}}).json()
        assert cleared["lastfm"]["has_key"] is False and cleared["lastfm"]["has_secret"] is True

    def test_rejects_unknown_sections_fields_and_wrong_types(self, empty_web) -> None:
        c = empty_web.client
        assert c.put("/api/settings", json={"nope": {"a": 1}}).status_code == 422
        assert c.put("/api/settings", json={"musicbrainz": {"app_name": "x"}}).status_code == 422
        assert c.put("/api/settings", json={"database": {"path": "/etc"}}).status_code == 422  # paths are not editable here
        assert c.put("/api/settings", json={"listenbrainz": {"enabled": "yes"}}).status_code == 422

    def test_a_bad_field_leaves_everything_unchanged(self, empty_web) -> None:
        c = empty_web.client
        response = c.put("/api/settings", json={"musicbrainz": {"contact": "new@example.com"}, "app": {"bogus": True}})
        assert response.status_code == 422
        assert c.get("/api/settings").json()["musicbrainz"]["contact"] == ""

    def test_folder_templates_are_validated(self, empty_web) -> None:
        c = empty_web.client
        ok = c.put("/api/settings", json={"library": {"canonical_scheme": "{artist}/{year} - {album}/{track:02d} {title}.{ext}"}})
        assert ok.status_code == 200
        for bad in ("{nonsense}/{title}.{ext}", "{title}", "/abs/{title}.{ext}", "../{title}.{ext}", "{track:zz}/{title}.{ext}"):
            response = c.put("/api/settings", json={"library": {"canonical_scheme": bad}})
            assert response.status_code == 422, bad


class TestLibraryFolders:
    def test_adding_a_folder_saves_it_and_starts_a_scan(self, empty_web, music_dir) -> None:
        c = empty_web.client
        response = c.post("/api/library/roots", json={"path": str(music_dir)})
        assert response.status_code == 201
        empty_web.ctx.jobs.wait(response.json()["job"]["id"], timeout=60)
        assert c.get("/api/tracks").json()["total"] == 9
        assert load_config(empty_web.ctx.config_path).library.roots == [str(music_dir.resolve())]

    def test_pasted_quoted_paths_work(self, empty_web, music_dir) -> None:
        response = empty_web.client.post("/api/library/roots", json={"path": f'  "{music_dir}"  '})
        assert response.status_code == 201
        empty_web.ctx.jobs.wait(response.json()["job"]["id"], timeout=60)

    def test_bad_and_duplicate_folders_are_refused(self, web, music_dir, tmp_path) -> None:
        c = web.client
        assert c.post("/api/library/roots", json={"path": str(tmp_path / "nope")}).status_code == 422
        assert c.post("/api/library/roots", json={"path": str(music_dir)}).status_code == 409

    def test_removing_a_folder_hides_its_tracks_but_never_touches_files(self, web, music_dir) -> None:
        c = web.client
        removed = c.post("/api/library/roots/remove", json={"path": str(music_dir)}).json()
        assert removed["roots"] == [] and removed["hidden_tracks"] == 9
        assert c.get("/api/tracks").json()["total"] == 0
        assert len(list(music_dir.rglob("*.mp3"))) == 9  # the music itself is untouched
        assert c.post("/api/library/roots/remove", json={"path": str(music_dir)}).status_code == 404

    def test_removing_one_of_several_folders_only_hides_that_one(self, web, tmp_path) -> None:
        other = tmp_path / "other"
        make_track(other, "x.mp3", title="Elsewhere", artist="Someone", album="Single")
        web.client.post("/api/library/roots", json={"path": str(other)})
        web.ctx.jobs.wait(web.ctx.jobs.list()[-1].id)
        assert web.client.get("/api/tracks").json()["total"] == 10
        web.client.post("/api/library/roots/remove", json={"path": str(other)})
        assert web.client.get("/api/tracks").json()["total"] == 9

    def test_scan_needs_a_folder(self, empty_web) -> None:
        assert empty_web.client.post("/api/library/scan").status_code == 409


class TestScanning:
    def test_rescan_is_incremental(self, web) -> None:
        result = scan(web)
        assert (result["added"], result["updated"], result["unchanged"]) == (0, 0, 9)

    def test_new_and_deleted_files_are_picked_up(self, web, music_dir) -> None:
        make_track(music_dir / "New Band" / "Debut", "01 - Hello.mp3", title="Hello", artist="New Band", album="Debut")
        (music_dir / "The Fernwoods" / "Back Roads" / "02 - Porch Light.mp3").unlink()
        result = scan(web)
        assert result["added"] == 1 and result["missing"] == 1
        names = [t["title"] for t in web.client.get("/api/tracks", params={"limit": 100}).json()["items"]]
        assert "Hello" in names and "Porch Light" not in names

    def test_an_unplugged_drive_is_skipped_not_treated_as_deleted(self, web, tmp_path) -> None:
        gone = tmp_path / "unplugged-drive"
        web.config.library.roots.append(str(gone))
        result = scan(web)
        assert result["offline_roots"] == [str(gone)]
        assert web.client.get("/api/tracks").json()["total"] == 9  # nothing was hidden

    def test_only_one_scan_runs_at_a_time(self, web) -> None:
        first = web.client.post("/api/library/scan").json()["job"]
        second = web.client.post("/api/library/scan").json()["job"]
        assert second["id"] == first["id"] or web.ctx.jobs.get(first["id"]).status == "done"

    def test_scan_progress_is_visible_through_the_jobs_api(self, web) -> None:
        job = web.client.post("/api/library/scan").json()["job"]
        web.ctx.jobs.wait(job["id"])
        finished = web.client.get(f"/api/jobs/{job['id']}").json()
        assert finished["status"] == "done" and finished["done"] == finished["total"] == 9
        assert any(j["id"] == job["id"] for j in web.client.get("/api/jobs").json()["jobs"])
        assert web.client.get("/api/jobs/nope").status_code == 404
        assert web.client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409

    def test_backup_makes_a_consistent_copy(self, web) -> None:
        import sqlite3

        web.client.post("/api/plays", json={"track_id": ids_by_title(web.client)["Wires"], "ms_played": 1000})
        backup = web.client.post("/api/system/backup").json()
        copy = sqlite3.connect(backup["path"])
        try:
            assert copy.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 9
            assert copy.execute("SELECT COUNT(*) FROM play_history").fetchone()[0] == 1
        finally:
            copy.close()

    def test_logs_endpoint_handles_a_missing_log_file(self, empty_web) -> None:
        assert empty_web.client.get("/api/system/logs").json()["lines"] == []


class TestConnectionTests:
    """The Test connection buttons: each tries the saved key against its own service and says plainly what happened."""

    def test_listenbrainz_says_who_the_token_belongs_to(self, empty_web, monkeypatch) -> None:
        c = empty_web.client
        assert c.post("/api/settings/test/listenbrainz").json()["ok"] is False  # nothing saved yet
        c.put("/api/settings", json={"listenbrainz": {"user_token": "tok-123", "username": "cayl"}})
        asked = []

        def good(token):
            asked.append(token)
            return {"valid": True, "user_name": "Cayl", "message": "Token valid."}

        monkeypatch.setattr(settings_router.listenbrainz_client, "validate_token", good)

        result = c.post("/api/settings/test/listenbrainz").json()

        assert result == {"ok": True, "message": "Connected as Cayl."} and asked == ["tok-123"]
        assert "tok-123" not in str(result)

    def test_listenbrainz_points_out_a_username_that_does_not_match_the_token(self, empty_web, monkeypatch) -> None:
        c = empty_web.client
        c.put("/api/settings", json={"listenbrainz": {"user_token": "tok-123", "username": "somebody_else"}})
        monkeypatch.setattr(settings_router.listenbrainz_client, "validate_token", lambda t: {"valid": True, "user_name": "cayl", "message": ""})
        result = c.post("/api/settings/test/listenbrainz").json()
        assert result["ok"] is True and "somebody_else" in result["message"] and "change it to cayl" in result["message"]

    def test_listenbrainz_explains_a_bad_token_and_a_missing_connection(self, empty_web, monkeypatch) -> None:
        c = empty_web.client
        c.put("/api/settings", json={"listenbrainz": {"user_token": "wrong"}})
        monkeypatch.setattr(settings_router.listenbrainz_client, "validate_token", lambda t: {"valid": False, "user_name": None, "message": ""})
        bad = c.post("/api/settings/test/listenbrainz").json()
        assert bad["ok"] is False and "does not recognise" in bad["message"]

        def offline(token):
            raise ListenBrainzError("no answer", status=None)

        monkeypatch.setattr(settings_router.listenbrainz_client, "validate_token", offline)
        gone = c.post("/api/settings/test/listenbrainz").json()
        assert gone["ok"] is False and "Can't reach ListenBrainz" in gone["message"]

    def test_lastfm_and_musicbrainz_are_tried_with_what_is_saved(self, empty_web, monkeypatch) -> None:
        c = empty_web.client
        assert c.post("/api/settings/test/lastfm").json()["ok"] is False
        assert "contact" in c.post("/api/settings/test/musicbrainz").json()["message"]

        c.put("/api/settings", json={"lastfm": {"api_key": "key-1"}, "musicbrainz": {"contact": "me@example.com"}})
        seen = {}
        monkeypatch.setattr(settings_router.lastfm_client, "check_key", lambda key: (seen.setdefault("lastfm", key) and False, "Last.fm says: Invalid API key"))
        monkeypatch.setattr(
            settings_router.musicbrainz_client, "ping",
            lambda app, version, contact: (seen.update(musicbrainz=(app, contact)) or True, "MusicBrainz is reachable and accepted the request."),
        )

        assert c.post("/api/settings/test/lastfm").json() == {"ok": False, "message": "Last.fm says: Invalid API key"}
        assert c.post("/api/settings/test/musicbrainz").json()["ok"] is True
        assert seen["lastfm"] == "key-1" and seen["musicbrainz"][1] == "me@example.com"

    def test_only_the_known_services_can_be_tested(self, empty_web) -> None:
        assert empty_web.client.post("/api/settings/test/http%3A%2F%2Fexample.com").status_code == 404
        assert empty_web.client.post("/api/settings/test/spotify").status_code == 404
