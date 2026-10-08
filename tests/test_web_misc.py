import sqlite3


def seed_recommendations(web) -> None:
    with web.ctx.db() as conn:
        conn.executemany(
            "INSERT INTO recommendations (artist_name, track_name, source, score, reason, musicbrainz_artist_id) "
            "VALUES (?, ?, 'listenbrainz_cf', ?, 'because you play X', ?)",
            [("Big Thief", "Not", 0.9, "abc-123"), ("Phoebe Bridgers", None, 0.5, None), ("Low Score", "Song", 0.1, None)],
        )
        conn.commit()


def test_recommendations_are_ranked_and_carry_listen_or_buy_links(web) -> None:
    seed_recommendations(web)
    data = web.client.get("/api/recommendations").json()
    assert [r["artist"] for r in data["items"]] == ["Big Thief", "Phoebe Bridgers", "Low Score"]
    labels = [link["label"] for link in data["items"][0]["links"]]
    assert labels == ["Bandcamp", "YouTube", "MusicBrainz"]
    assert "Big+Thief+Not" in data["items"][0]["links"][0]["url"]
    assert [link["label"] for link in data["items"][1]["links"]] == ["Bandcamp", "YouTube"]  # no MBID, no link


def test_triage_moves_items_between_lists_and_can_be_undone(web) -> None:
    seed_recommendations(web)
    c = web.client
    first = c.get("/api/recommendations").json()["items"][0]["id"]
    assert c.post(f"/api/recommendations/{first}/status", json={"status": "accepted"}).status_code == 200
    data = c.get("/api/recommendations").json()
    assert first not in [r["id"] for r in data["items"]] and data["counts"] == {"new": 2, "accepted": 1}
    assert [r["artist"] for r in c.get("/api/recommendations", params={"status": "accepted"}).json()["items"]] == ["Big Thief"]

    c.post(f"/api/recommendations/{first}/status", json={"status": "new"})
    assert len(c.get("/api/recommendations").json()["items"]) == 3
    assert c.post(f"/api/recommendations/{first}/status", json={"status": "bogus"}).status_code == 422
    assert c.post("/api/recommendations/9999/status", json={"status": "owned"}).status_code == 404
    assert c.get("/api/recommendations", params={"status": "weird"}).status_code == 422


def test_history_top_artists_and_recent(web) -> None:
    with web.ctx.db() as conn:
        for i in range(3):
            conn.execute("INSERT INTO play_history (source, played_at_epoch, raw_artist_name, raw_track_name) "
                         "VALUES ('spotify_import', ?, 'Popular', 'S')", (1_600_000_000 + i,))
        conn.execute("INSERT INTO play_history (source, played_at_epoch, raw_artist_name, raw_track_name) "
                     "VALUES ('spotify_import', 1_700_000_000, 'Other', 'T')".replace("1_700_000_000", "1700000000"))
        conn.commit()
    top = web.client.get("/api/history/top-artists").json()
    assert top["total_plays"] == 4 and top["items"][0] == {"name": "Popular", "plays": 3}
    assert web.client.get("/api/history/top-artists", params={"days": 1}).json()["items"] == []  # all are old
    recent = web.client.get("/api/history/recent", params={"limit": 2}).json()["items"]
    assert [r["artist"] for r in recent] == ["Other", "Popular"]


def test_devices_list_and_volumes(web) -> None:
    with web.ctx.db() as conn:
        conn.execute("INSERT INTO devices (label, last_seen_mount_path, created_at) VALUES ('My DAP', 'E:\\\\', '2024-01-01')")
        conn.commit()
    devices = web.client.get("/api/devices").json()["items"]
    assert devices[0]["label"] == "My DAP" and devices[0]["synced"] == 0
    volumes = web.client.get("/api/devices/volumes").json()["items"]
    assert isinstance(volumes, list) and all({"mount_path", "free", "total", "removable"} <= v.keys() for v in volumes)


class TestRefreshRecommendations:
    def _configure(self, web, **listenbrainz) -> None:
        web.client.put("/api/settings", json={"listenbrainz": listenbrainz})

    def test_without_a_username_the_job_explains_what_to_set(self, web) -> None:
        job = web.client.post("/api/recommendations/refresh", json={}).json()["job"]
        finished = web.ctx.jobs.wait(job["id"])
        assert finished.status == "error" and "ListenBrainz username" in finished.error

    def test_fetch_filters_owned_music_ranks_and_saves(self, web, monkeypatch) -> None:
        from musictoolkit.recommend import engine

        self._configure(web, username="cayl")
        monkeypatch.setattr("musictoolkit.recommend.service.musicbrainz_client.configure", lambda *a: None)
        monkeypatch.setattr(engine, "fetch_listenbrainz_candidates", lambda user, token, limit: [
            engine.Candidate("Mara Quinn", "Wires", None, None, "listenbrainz_cf", 0.9, "owned already"),
            engine.Candidate("Brand New Band", "Fresh Song", None, None, "listenbrainz_cf", 0.7, "cf"),
            engine.Candidate("Another One", "Tune", None, None, "listenbrainz_cf", 0.3, "cf"),
        ])
        job = web.client.post("/api/recommendations/refresh", json={"limit": 10}).json()["job"]
        finished = web.ctx.jobs.wait(job["id"])
        assert finished.status == "done"
        assert (finished.result["fetched"], finished.result["already_owned"], finished.result["saved"]) == (3, 1, 2)
        listed = web.client.get("/api/recommendations").json()["items"]
        assert [r["artist"] for r in listed] == ["Brand New Band", "Another One"]  # owned music never appears

    def test_a_second_refresh_does_not_duplicate(self, web, monkeypatch) -> None:
        from musictoolkit.recommend import engine

        self._configure(web, username="cayl")
        monkeypatch.setattr("musictoolkit.recommend.service.musicbrainz_client.configure", lambda *a: None)
        monkeypatch.setattr(engine, "fetch_listenbrainz_candidates", lambda *a: [
            engine.Candidate("Brand New Band", "Fresh Song", None, None, "listenbrainz_cf", 0.7, "cf")])
        for _ in range(2):
            web.ctx.jobs.wait(web.client.post("/api/recommendations/refresh", json={}).json()["job"]["id"])
        assert web.client.get("/api/recommendations").json()["total"] == 1

    def test_a_failing_service_is_reported_not_swallowed(self, web, monkeypatch) -> None:
        from musictoolkit.recommend import engine

        self._configure(web, username="cayl")
        monkeypatch.setattr("musictoolkit.recommend.service.musicbrainz_client.configure", lambda *a: None)
        def boom(*a):
            raise RuntimeError("503 from listenbrainz")
        monkeypatch.setattr(engine, "fetch_listenbrainz_candidates", boom)
        finished = web.ctx.jobs.wait(web.client.post("/api/recommendations/refresh", json={}).json()["job"]["id"])
        assert finished.status == "done" and "503 from listenbrainz" in finished.result["messages"][0]
