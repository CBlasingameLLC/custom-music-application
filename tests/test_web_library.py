import json

import pytest

from tests.conftest import ids_by_title


def rules(*items, match="all") -> str:
    return json.dumps({"match": match, "rules": list(items)})


def titles(client, **params) -> list[str]:
    return [t["title"] for t in client.get("/api/tracks", params={"limit": 1000, **params}).json()["items"]]


class TestTracks:
    def test_scan_populates_the_library(self, web) -> None:
        listing = web.client.get("/api/tracks").json()
        assert listing["total"] == 9
        first = listing["items"][0]
        assert {"id", "title", "artist", "album", "genre", "year", "duration", "rating", "favorite", "plays",
                "album_key"} <= first.keys()

    def test_paging_returns_disjoint_pages_and_the_same_total(self, web) -> None:
        page1 = web.client.get("/api/tracks", params={"limit": 4, "offset": 0, "sort": "title"}).json()
        page2 = web.client.get("/api/tracks", params={"limit": 4, "offset": 4, "sort": "title"}).json()
        assert page1["total"] == page2["total"] == 9
        assert not {t["id"] for t in page1["items"]} & {t["id"] for t in page2["items"]}
        assert [t["title"] for t in page1["items"] + page2["items"]] == sorted(
            titles(web.client), key=str.lower
        )[:8]

    @pytest.mark.parametrize(
        ("sort", "direction", "expected_first"),
        [("title", "asc", "Drift"), ("title", "desc", "Wires"), ("year", "asc", "Gravel Dust"),
         ("year", "desc", "Salt and Stone"), ("album", "asc", "Gravel Dust")],
    )
    def test_sorting(self, web, sort, direction, expected_first) -> None:
        assert titles(web.client, sort=sort, dir=direction)[0] == expected_first

    def test_default_order_keeps_albums_together_in_track_order(self, web) -> None:
        order = titles(web.client)
        # artist, then album alphabetically (Low Tide < Northern Lights), then track number
        assert order[:4] == ["Salt and Stone", "Glacier", "Polar Night", "Drift"]

    def test_search_matches_any_field_and_all_words(self, web) -> None:
        assert titles(web.client, q="neon") == ["Neon Rain"]
        assert set(titles(web.client, q="fernwoods")) == {"Gravel Dust", "Porch Light"}
        assert titles(web.client, q="mara wires") == ["Wires"]  # words may hit different fields
        assert titles(web.client, q="folk 2015") != []
        assert titles(web.client, q="zzzz") == []

    def test_search_treats_sql_wildcards_literally(self, web) -> None:
        assert titles(web.client, q="%") == []
        assert titles(web.client, q="_") == []

    def test_ids_endpoint_matches_listing_order(self, web) -> None:
        ids = web.client.get("/api/tracks/ids", params={"sort": "title"}).json()["ids"]
        listing = web.client.get("/api/tracks", params={"sort": "title", "limit": 100}).json()["items"]
        assert ids == [t["id"] for t in listing]

    def test_by_ids_preserves_requested_order_and_skips_unknown(self, web) -> None:
        by_title = ids_by_title(web.client)
        wanted = [by_title["Wires"], 99999, by_title["Drift"], by_title["Wires"]]
        got = web.client.post("/api/tracks/by-ids", json={"ids": wanted}).json()["items"]
        assert [t["title"] for t in got] == ["Wires", "Drift", "Wires"]

    def test_single_track_detail_includes_path(self, web) -> None:
        track_id = ids_by_title(web.client)["Glacier"]
        detail = web.client.get(f"/api/tracks/{track_id}").json()
        assert detail["path"].endswith("01 - Glacier.mp3") and detail["folder"]
        assert web.client.get("/api/tracks/424242").status_code == 404

    def test_missing_files_are_hidden(self, web) -> None:
        with web.ctx.db() as conn:
            conn.execute("UPDATE tracks SET is_missing = 1 WHERE title = 'Wires'")
            conn.commit()
        assert "Wires" not in titles(web.client)
        assert web.client.get("/api/tracks").json()["total"] == 8


class TestRules:
    def test_text_operators(self, web) -> None:
        c = web.client
        assert set(titles(c, rules=rules({"field": "genre", "op": "is", "value": "folk"}))) == {"Gravel Dust", "Porch Light"}
        assert len(titles(c, rules=rules({"field": "genre", "op": "is_not", "value": "Folk"}))) == 7
        assert set(titles(c, rules=rules({"field": "artist", "op": "contains", "value": "VALE"}))) == {
            "Glacier", "Polar Night", "Drift", "Salt and Stone"}
        assert titles(c, rules=rules({"field": "title", "op": "starts_with", "value": "sig"})) == ["Signal Fade"]
        assert len(titles(c, rules=rules({"field": "title", "op": "not_contains", "value": "a"}))) == 3
        assert len(titles(c, rules=rules({"field": "genre", "op": "is_any", "value": ["Folk", "Ambient"]}))) == 3
        assert len(titles(c, rules=rules({"field": "genre", "op": "not_empty"}))) == 9
        assert titles(c, rules=rules({"field": "genre", "op": "empty"})) == []

    def test_numeric_operators(self, web) -> None:
        c = web.client
        assert len(titles(c, rules=rules({"field": "year", "op": ">=", "value": 2019}))) == 7
        assert len(titles(c, rules=rules({"field": "year", "op": "between", "value": [2015, 2019]}))) == 5
        assert len(titles(c, rules=rules({"field": "year", "op": "!=", "value": 2021}))) == 6
        assert titles(c, rules=rules({"field": "track", "op": "=", "value": 3, }, {"field": "year", "op": "=", "value": 2019})) == ["Drift"]

    def test_match_any_versus_all(self, web) -> None:
        both = rules({"field": "genre", "op": "is", "value": "Folk"}, {"field": "year", "op": "=", "value": 2021})
        assert titles(web.client, rules=both) == []
        either = rules({"field": "genre", "op": "is", "value": "Folk"}, {"field": "year", "op": "=", "value": 2021},
                       match="any")
        assert len(titles(web.client, rules=either)) == 5

    def test_rating_favorite_and_play_based_fields(self, web) -> None:
        c = web.client
        by_title = ids_by_title(c)
        c.post("/api/tracks/bulk", json={"ids": [by_title["Wires"], by_title["Drift"]], "rating": 5})
        c.post("/api/tracks/bulk", json={"ids": [by_title["Drift"]], "favorite": True})
        c.post("/api/plays", json={"track_id": by_title["Wires"], "ms_played": 60000})

        assert set(titles(c, rules=rules({"field": "rating", "op": ">=", "value": 4}))) == {"Wires", "Drift"}
        assert titles(c, rules=rules({"field": "favorite", "op": "is", "value": True})) == ["Drift"]
        assert len(titles(c, rules=rules({"field": "favorite", "op": "is", "value": False}))) == 8
        assert titles(c, rules=rules({"field": "plays", "op": ">=", "value": 1})) == ["Wires"]
        assert titles(c, rules=rules({"field": "unplayed", "op": "is", "value": False})) == ["Wires"]
        assert len(titles(c, rules=rules({"field": "not_played_within_days", "op": "is", "value": 30}))) == 8
        assert titles(c, rules=rules({"field": "played_within_days", "op": "is", "value": 1})) == ["Wires"]
        assert len(titles(c, rules=rules({"field": "added_within_days", "op": "is", "value": 1}))) == 9
        assert titles(c, rules=rules({"field": "added_within_days", "op": "is", "value": 0})) == []

    def test_missing_tags_rule(self, web, tmp_path) -> None:
        from tests.conftest import make_track

        make_track(tmp_path / "music" / "loose", "untagged.mp3")
        web.client.post("/api/library/scan")
        web.ctx.jobs.wait(web.ctx.jobs.list()[-1].id)
        # the fixture copy keeps a title tag only if we set one; make_track clears all tags
        found = titles(web.client, rules=rules({"field": "missing_tags", "op": "is", "value": True}))
        assert found == ["untagged"]  # title falls back to the file name

    @pytest.mark.parametrize(
        "bad",
        [
            {"field": "nope", "op": "is", "value": 1},
            {"field": "genre", "op": "regex", "value": "x"},
            {"field": "year", "op": "contains", "value": 1},
            {"field": "year", "op": ">", "value": "abc"},
            {"field": "year", "op": "between", "value": [1]},
            {"field": "genre", "op": "is_any", "value": []},
            {"field": "genre", "op": "is_any", "value": "Folk"},
            {"field": ["genre"], "op": "is", "value": "x"},
            {"field": {"a": 1}, "op": "is", "value": 1},
            {"field": "genre", "op": ["is"], "value": "x"},
            {"field": "year", "op": ">", "value": [1, 2]},
        ],
    )
    def test_invalid_rules_are_rejected_with_a_message(self, web, bad) -> None:
        response = web.client.get("/api/tracks", params={"rules": rules(bad)})
        assert response.status_code == 422
        assert response.json()["detail"]

    def test_rules_are_never_interpolated_into_sql(self, web) -> None:
        evil = rules({"field": "title", "op": "is", "value": "x' OR '1'='1"})
        assert titles(web.client, rules=evil) == []
        assert web.client.get("/api/tracks", params={"rules": "{not json"}).status_code == 422
        assert web.client.get("/api/tracks", params={"sort": "title; DROP TABLE tracks"}).status_code == 200
        assert web.client.get("/api/tracks").json()["total"] == 9


class TestAlbumsAndArtists:
    def test_albums_group_and_count(self, web) -> None:
        albums = {a["album"]: a for a in web.client.get("/api/albums").json()["items"]}
        assert set(albums) == {"Northern Lights", "Low Tide", "Back Roads", "Static Hearts"}
        assert albums["Northern Lights"]["tracks"] == 3 and albums["Northern Lights"]["year"] == 2019
        assert albums["Static Hearts"]["artist"] == "Mara Quinn"

    def test_albums_sort_filter_and_search(self, web) -> None:
        c = web.client
        by_year = [a["album"] for a in c.get("/api/albums", params={"sort": "year", "dir": "desc"}).json()["items"]]
        assert by_year[0] == "Low Tide"
        folk = c.get("/api/albums", params={"rules": rules({"field": "genre", "op": "is", "value": "Folk"})}).json()
        assert [a["album"] for a in folk["items"]] == ["Back Roads"]
        assert [a["album"] for a in c.get("/api/albums", params={"q": "static"}).json()["items"]] == ["Static Hearts"]
        assert c.get("/api/albums", params={"sort": "random"}).json()["total"] == 4

    def test_album_detail_has_tracks_in_order(self, web) -> None:
        key = next(a["key"] for a in web.client.get("/api/albums").json()["items"] if a["album"] == "Northern Lights")
        album = web.client.get(f"/api/albums/{key}").json()
        assert [t["title"] for t in album["tracks"]] == ["Glacier", "Polar Night", "Drift"]
        assert album["artist"] == "Aurora Vale" and album["genres"] == ["Electronic"]
        assert web.client.get("/api/albums/bm90LWEtcmVhbC1rZXk").status_code == 404

    def test_tracks_can_be_listed_by_album_and_by_artist(self, web) -> None:
        key = next(a["key"] for a in web.client.get("/api/albums").json()["items"] if a["album"] == "Back Roads")
        assert titles(web.client, album=key) == ["Gravel Dust", "Porch Light"]
        assert len(titles(web.client, artist="Aurora Vale")) == 4

    def test_artists_and_artist_page(self, web) -> None:
        artists = {a["name"]: a for a in web.client.get("/api/artists").json()["items"]}
        assert artists["Aurora Vale"]["tracks"] == 4 and artists["Aurora Vale"]["albums"] == 2
        page = web.client.get("/api/artist", params={"name": "Aurora Vale"}).json()
        assert [a["album"] for a in page["albums"]] == ["Northern Lights", "Low Tide"]  # chronological
        assert len(page["top_tracks"]) == 4
        assert web.client.get("/api/artist", params={"name": "Nobody"}).status_code == 404

    def test_untagged_files_group_under_unknown(self, web, tmp_path) -> None:
        from tests.conftest import make_track

        make_track(tmp_path / "music" / "loose", "mystery.mp3")
        web.client.post("/api/library/scan")
        web.ctx.jobs.wait(web.ctx.jobs.list()[-1].id)
        names = {a["name"] for a in web.client.get("/api/artists").json()["items"]}
        assert "Unknown Artist" in names


class TestFacetsSearchHome:
    def test_facets(self, web) -> None:
        facets = web.client.get("/api/facets").json()
        assert {g["name"]: g["count"] for g in facets["genres"]} == {
            "Electronic": 3, "Folk": 2, "Indie Rock": 3, "Ambient": 1}
        assert facets["totals"]["tracks"] == 9 and facets["totals"]["albums"] == 4 and facets["totals"]["artists"] == 3
        assert [y["year"] for y in facets["years"]] == [2022, 2021, 2019, 2015]

    def test_global_search_groups_results(self, web) -> None:
        web.client.post("/api/playlists", json={"name": "Neon nights"})
        result = web.client.get("/api/search", params={"q": "neon"}).json()
        assert [t["title"] for t in result["tracks"]] == ["Neon Rain"]
        assert result["albums"][0]["album"] == "Static Hearts"
        assert result["artists"][0]["name"] == "Mara Quinn"
        assert [p["name"] for p in result["playlists"]] == ["Neon nights"]

    def test_home_rows_reflect_listening(self, web) -> None:
        home = web.client.get("/api/home").json()
        assert home["recent"] == [] and len(home["recently_added"]) == 4 and home["totals"]["tracks"] == 9
        by_title = ids_by_title(web.client)
        web.client.post("/api/plays", json={"track_id": by_title["Drift"], "ms_played": 90000})
        web.client.post("/api/tracks/bulk", json={"ids": [by_title["Glacier"]], "favorite": True})
        home = web.client.get("/api/home").json()
        assert [t["title"] for t in home["recent"]] == ["Drift"]
        assert [t["title"] for t in home["favorites"]] == ["Glacier"]
        assert home["totals"]["plays"] == 1

    def test_home_shelves_are_ordered_by_plays_and_by_recency(self, web) -> None:
        by_title = ids_by_title(web.client)
        for title, started in (("Drift", 3000), ("Glacier", 1000), ("Glacier", 5000), ("Wires", 4000)):
            web.client.post("/api/plays", json={"track_id": by_title[title], "ms_played": 90000, "started_at": 1_700_000_000 + started})

        home = web.client.get("/api/home").json()

        assert [t["title"] for t in home["most_played"]] == ["Glacier", "Drift", "Wires"]  # two plays, then one each in the library's order
        assert [t["title"] for t in home["recent"]] == ["Glacier", "Wires", "Drift"]
        assert [(t["title"], t["plays"]) for t in home["most_played"]][0] == ("Glacier", 2)

    def test_home_offers_each_album_once_and_random_albums_are_real_ones(self, web) -> None:
        home = web.client.get("/api/home").json()

        albums = {"Northern Lights": 3, "Low Tide": 1, "Back Roads": 2, "Static Hearts": 3}
        assert {a["album"]: a["tracks"] for a in home["recently_added"]} == albums
        assert {a["album"]: a["tracks"] for a in home["random_albums"]} == albums
        assert all(a["cover_track_id"] for a in home["random_albums"] + home["recently_added"])


class TestRatingsAndFavorites:
    def test_patch_sets_and_clears(self, web) -> None:
        track_id = ids_by_title(web.client)["Wires"]
        updated = web.client.patch(f"/api/tracks/{track_id}", json={"rating": 4, "favorite": True}).json()
        assert updated["rating"] == 4 and updated["favorite"] is True
        cleared = web.client.patch(f"/api/tracks/{track_id}", json={"rating": 0, "favorite": False}).json()
        assert cleared["rating"] == 0 and cleared["favorite"] is False

    def test_validation(self, web) -> None:
        track_id = ids_by_title(web.client)["Wires"]
        assert web.client.patch(f"/api/tracks/{track_id}", json={"rating": 9}).status_code == 422
        assert web.client.patch(f"/api/tracks/{track_id}", json={}).status_code == 422
        assert web.client.patch("/api/tracks/424242", json={"rating": 3}).status_code == 404
        assert web.client.post("/api/tracks/bulk", json={"ids": []}).status_code == 422

    def test_bulk_update_many(self, web) -> None:
        ids = list(ids_by_title(web.client).values())
        assert web.client.post("/api/tracks/bulk", json={"ids": ids, "rating": 3}).json() == {"updated": 9}
        assert all(t["rating"] == 3 for t in web.client.get("/api/tracks").json()["items"])
