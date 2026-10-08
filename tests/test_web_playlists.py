import json

from tests.conftest import ids_by_title


def make_playlist(client, name="Road trip", track_titles=(), **extra) -> dict:
    ids = ids_by_title(client)
    body = {"name": name, "track_ids": [ids[t] for t in track_titles], **extra}
    response = client.post("/api/playlists", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def playlist_titles(client, playlist_id: int) -> list[str]:
    items = client.get("/api/tracks", params={"playlist": playlist_id, "limit": 1000}).json()["items"]
    return [t["title"] for t in items]


def test_create_list_rename_delete(web) -> None:
    c = web.client
    created = make_playlist(c, "Road trip", ["Wires", "Drift"])
    assert created["kind"] == "manual" and created["tracks"] == 2 and len(created["cover_track_ids"]) == 2
    assert [p["name"] for p in c.get("/api/playlists").json()["items"]] == ["Road trip"]

    renamed = c.patch(f"/api/playlists/{created['id']}", json={"name": "Road trip 2"}).json()
    assert renamed["name"] == "Road trip 2"

    assert c.delete(f"/api/playlists/{created['id']}").status_code == 200
    assert c.get(f"/api/playlists/{created['id']}").status_code == 404
    with web.ctx.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 0


def test_playlist_keeps_its_own_order_not_the_library_order(web) -> None:
    created = make_playlist(web.client, "Mix", ["Wires", "Glacier", "Porch Light"])
    assert playlist_titles(web.client, created["id"]) == ["Wires", "Glacier", "Porch Light"]


def test_add_inserts_at_a_position_and_allows_duplicates(web) -> None:
    c = web.client
    ids = ids_by_title(c)
    created = make_playlist(c, "Mix", ["Wires", "Drift"])
    c.post(f"/api/playlists/{created['id']}/tracks", json={"ids": [ids["Glacier"]], "position": 1})
    c.post(f"/api/playlists/{created['id']}/tracks", json={"ids": [ids["Wires"]]})
    assert playlist_titles(c, created["id"]) == ["Wires", "Glacier", "Drift", "Wires"]


def test_add_skips_unknown_tracks(web) -> None:
    created = make_playlist(web.client, "Mix")
    result = web.client.post(f"/api/playlists/{created['id']}/tracks", json={"ids": [ids_by_title(web.client)["Wires"], 99999]})
    assert result.json() == {"added": 1, "skipped": 1}


def test_remove_by_position_handles_duplicates(web) -> None:
    c = web.client
    created = make_playlist(c, "Mix", ["Wires", "Drift", "Wires"])
    c.post(f"/api/playlists/{created['id']}/tracks/remove", json={"positions": [2]})
    assert playlist_titles(c, created["id"]) == ["Wires", "Drift"]  # only the second Wires went


def test_move_reorders(web) -> None:
    c = web.client
    created = make_playlist(c, "Mix", ["Wires", "Drift", "Glacier"])
    c.post(f"/api/playlists/{created['id']}/tracks/move", json={"from_position": 2, "to_position": 0})
    assert playlist_titles(c, created["id"]) == ["Glacier", "Wires", "Drift"]
    assert c.post(f"/api/playlists/{created['id']}/tracks/move", json={"from_position": 9, "to_position": 0}).status_code == 422


def test_positions_are_reported_for_removal_and_dragging(web) -> None:
    created = make_playlist(web.client, "Mix", ["Wires", "Drift"])
    items = web.client.get("/api/tracks", params={"playlist": created["id"]}).json()["items"]
    assert [t["pos"] for t in items] == [0, 1]


def test_smart_playlist_fills_itself_from_rules(web) -> None:
    c = web.client
    rules = {"match": "all", "rules": [{"field": "genre", "op": "is", "value": "Folk"}]}
    smart = make_playlist(c, "Folk", kind="smart", rules=rules)
    assert smart["kind"] == "smart" and smart["tracks"] == 2 and smart["rules"] == rules
    assert set(playlist_titles(c, smart["id"])) == {"Gravel Dust", "Porch Light"}

    c.patch(f"/api/playlists/{smart['id']}", json={"rules": {"match": "all", "rules": [
        {"field": "genre", "op": "is_any", "value": ["Folk", "Ambient"]}]}})
    assert len(playlist_titles(c, smart["id"])) == 3  # live: recomputed every time it is opened


def test_smart_playlists_reject_bad_rules_and_manual_edits(web) -> None:
    c = web.client
    bad = c.post("/api/playlists", json={"name": "x", "kind": "smart", "rules": {"rules": [{"field": "nope"}]}})
    assert bad.status_code == 422
    smart = make_playlist(c, "Folk", kind="smart", rules={"rules": [{"field": "genre", "op": "is", "value": "Folk"}]})
    assert c.post(f"/api/playlists/{smart['id']}/tracks", json={"ids": [1]}).status_code == 409
    manual = make_playlist(c, "Plain")
    assert c.patch(f"/api/playlists/{manual['id']}", json={"rules": {"rules": []}}).status_code == 409


def test_playlist_name_is_required(web) -> None:
    assert web.client.post("/api/playlists", json={"name": ""}).status_code == 422


def test_export_m3u8(web) -> None:
    created = make_playlist(web.client, "Road/trip: 1", ["Wires", "Drift"])
    response = web.client.get(f"/api/playlists/{created['id']}/export.m3u8")
    assert response.status_code == 200
    assert 'filename="Road_trip_ 1.m3u8"' in response.headers["content-disposition"]
    lines = response.text.splitlines()
    assert lines[0] == "#EXTM3U" and lines[1].startswith("#EXTINF:") and "Mara Quinn - Wires" in lines[1]
    assert lines[2].endswith("01 - Wires.mp3") and lines[4].endswith("03 - Drift.mp3")


def test_import_m3u_matches_scanned_tracks_and_reports_the_rest(web, tmp_path) -> None:
    c = web.client
    wires = c.get(f"/api/tracks/{ids_by_title(c)['Wires']}").json()["path"]
    m3u = tmp_path / "Imported.m3u8"
    m3u.write_text(f"#EXTM3U\n{wires}\n{tmp_path / 'not scanned.mp3'}\n", encoding="utf-8")

    result = c.post("/api/playlists/import", json={"path": str(m3u)}).json()
    assert (result["entries"], result["matched"], result["unmatched"]) == (2, 1, 1)
    assert result["playlist"]["name"] == "Imported"
    assert playlist_titles(c, result["playlist"]["id"]) == ["Wires"]


def test_import_rejects_non_playlists(web, tmp_path) -> None:
    (tmp_path / "x.txt").write_text("hi")
    assert web.client.post("/api/playlists/import", json={"path": str(tmp_path / "x.txt")}).status_code == 422
    assert web.client.post("/api/playlists/import", json={"path": str(tmp_path / "gone.m3u")}).status_code == 422


def test_clicking_a_column_sorts_a_playlist_by_it_and_default_is_its_own_order(web) -> None:
    c = web.client
    created = make_playlist(c, "Mix", ["Wires", "Glacier", "Porch Light"])
    own = [t["title"] for t in c.get("/api/tracks", params={"playlist": created["id"]}).json()["items"]]
    by_title = [t["title"] for t in c.get("/api/tracks", params={"playlist": created["id"], "sort": "title"}).json()["items"]]
    by_title_desc = [t["title"] for t in c.get("/api/tracks", params={"playlist": created["id"], "sort": "title", "dir": "desc"}).json()["items"]]
    assert own == ["Wires", "Glacier", "Porch Light"]
    assert by_title == ["Glacier", "Porch Light", "Wires"] and by_title_desc == by_title[::-1]
