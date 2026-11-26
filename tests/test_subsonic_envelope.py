"""The shape of a Subsonic answer (XML and JSON from one structure), the ids a client holds, the parameters it sends, and how /rest finds a call."""

from __future__ import annotations

import json
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from musictoolkit import __version__
from musictoolkit.web.subsonic import ids, render
from musictoolkit.web.subsonic.errors import NOT_FOUND, SubsonicError, missing, not_found
from musictoolkit.web.subsonic.params import Params
from tests.subsonic_support import NAMESPACE, Rest, canonical_json, canonical_xml, tag

HEAD = f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1" type="musictoolkit" serverVersion="{__version__}" openSubsonic="true"'


# ------------------------------------------------------------------------------------------------------------------------------ the envelope


def test_an_answer_with_nothing_in_it_says_the_status_the_version_and_what_this_server_is() -> None:
    assert render.to_xml(render.envelope()).decode() == HEAD + "/>"
    assert json.loads(render.to_json(render.envelope())) == {
        "subsonic-response": {"status": "ok", "version": "1.16.1", "type": "musictoolkit", "serverVersion": __version__, "openSubsonic": True}
    }


def test_a_failure_says_failed_and_the_number_and_the_words() -> None:
    answer = render.failure(40, "Wrong username or password")
    assert json.loads(render.to_json(answer))["subsonic-response"] == {
        "status": "failed", "version": "1.16.1", "type": "musictoolkit", "serverVersion": __version__, "openSubsonic": True,
        "error": {"code": 40, "message": "Wrong username or password"},
    }
    assert render.to_xml(answer).decode() == HEAD.replace('status="ok"', 'status="failed"') + '><error code="40" message="Wrong username or password"/></subsonic-response>'


def test_the_payload_of_a_call_goes_inside_the_envelope_and_not_over_it() -> None:
    answer = render.envelope({"license": {"valid": True}, "status": "nope"})  # (a key of the payload that is the envelope's own: the payload's wins, as it is written last)
    assert answer["license"] == {"valid": True}
    assert set(answer) >= {"status", "version", "type", "serverVersion", "openSubsonic", "license"}


# ---------------------------------------------------------------------------------------------------------------------------------- the XML


def xml_of(payload: dict) -> str:
    return render.to_xml(render.envelope(payload)).decode()


def test_plain_values_are_attributes_and_nested_ones_are_child_elements() -> None:
    xml = xml_of({"album": {"id": "al-1", "songCount": 2, "starred": False, "song": [{"id": "1", "track": 1}, {"id": "2", "track": 2}], "artist": {"name": "A"}}})
    assert xml == HEAD + '><album id="al-1" songCount="2" starred="false"><song id="1" track="1"/><song id="2" track="2"/><artist name="A"/></album></subsonic-response>'


def test_the_key_value_is_the_text_of_the_element() -> None:
    assert xml_of({"line": {"start": 100, "value": "la la"}}) == HEAD + '><line start="100">la la</line></subsonic-response>'
    assert xml_of({"note": {"value": "just text"}}) == HEAD + "><note>just text</note></subsonic-response>"


def test_a_list_of_plain_values_is_one_element_for_each() -> None:
    assert xml_of({"user": {"name": "me", "folder": [1, 2]}}) == HEAD + '><user name="me"><folder>1</folder><folder>2</folder></user></subsonic-response>'


def test_what_is_none_is_left_out_of_the_xml_and_of_the_json() -> None:
    payload = {"song": {"id": "1", "genre": None, "genres": [None, {"name": "Rock"}], "artists": [None]}}
    assert xml_of(payload) == HEAD + '><song id="1"><genres name="Rock"/></song></subsonic-response>'
    assert json.loads(render.to_json(render.envelope(payload)))["subsonic-response"]["song"] == {"id": "1", "genres": [{"name": "Rock"}], "artists": []}


def test_the_characters_that_xml_does_not_allow_are_dropped_and_the_ones_it_reserves_are_escaped() -> None:
    xml = xml_of({"song": {"title": 'a<b>&"c\'\x00\x08\x0b\x1f d￾￿', "value": "x < y & z"}})
    assert 'title="a&lt;b&gt;&amp;&quot;c\'' in xml and " d\"" in xml
    assert "\x00" not in xml and "\x08" not in xml and "\x0b" not in xml and "\x1f" not in xml and "￾" not in xml and "￿" not in xml
    assert ">x &lt; y &amp; z</song>" in xml
    ElementTree.fromstring(xml.encode())  # (and it is a document that can be read)


def test_what_xml_does_allow_stays_even_when_it_is_unusual() -> None:
    xml = xml_of({"song": {"title": "tab\there é 名前 \U0001f600 ퟿�"}})
    parsed = ElementTree.fromstring(xml.encode())
    assert parsed[0].attrib["title"] == "tab\there é 名前 \U0001f600 ퟿�"


def test_the_xml_is_in_the_namespace_of_the_protocol_and_utf_8() -> None:
    document = render.to_xml(render.envelope({"song": {"title": "é"}}))
    assert document.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    root = ElementTree.fromstring(document)
    assert root.tag == NAMESPACE + "subsonic-response" and root[0].attrib["title"] == "é"


# --------------------------------------------------------------------------------------------------------------------------------- the JSON


def test_the_json_is_compact_and_keeps_the_letters_as_they_are() -> None:
    text = render.to_json(render.envelope({"song": {"title": "é名"}})).decode()
    assert '"title":"é名"' in text and ", " not in text and ": " not in text


def test_jsonp_wraps_the_answer_in_the_function_the_client_named() -> None:
    text = render.to_json(render.envelope(), "handle").decode()
    assert text.startswith("handle(") and text.endswith(")") and json.loads(text[len("handle("):-1])["subsonic-response"]["status"] == "ok"
    assert render.to_json(render.envelope(), "my.app.$cb_1").startswith(b"my.app.$cb_1(")


@pytest.mark.parametrize("callback", ["1abc", "a b", "a(b)", "a;b", "a-b", "</script>", "a\nb", "a'b", "a.", ".a", "a..b", "a.1b"])
def test_a_name_that_is_not_a_function_is_not_written_into_the_answer(callback: str) -> None:
    with pytest.raises(ValueError, match="^not a function name$"):
        render.to_json(render.envelope(), callback)


def test_no_callback_is_plain_json() -> None:
    assert render.to_json(render.envelope(), "").startswith(b'{"subsonic-response"') and render.to_json(render.envelope(), None).startswith(b'{"subsonic-response"')


# -------------------------------------------------------------------------------------------------------------------------------------- ids


@pytest.mark.parametrize("name", ["A", "The Fernwoods", "Mara Quinn", "é名前 \U0001f600", "a/b+c=d", "x" * 300, "?", "~"])
def test_an_artist_id_gives_the_name_back(name: str) -> None:
    given = ids.artist_id(name)
    assert given.startswith("ar-") and "=" not in given and "/" not in given and "+" not in given
    assert ids.kind_of(given) == "artist" and ids.to_artist(given) == name


def test_the_ids_of_songs_albums_and_playlists_are_told_apart_by_what_they_start_with() -> None:
    assert (ids.song_id(12), ids.playlist_id(7), ids.album_id("abc")) == ("12", "pl-7", "al-abc")
    assert [ids.kind_of(given) for given in ("12", "al-abc", "ar-QQ", "pl-7")] == ["song", "album", "artist", "playlist"]
    assert ids.to_song("12") == 12 and ids.to_album("al-abc") == "abc" and ids.to_playlist("pl-7") == 7


@pytest.mark.parametrize("given", ["", "abc", "12a", "-1", "1.5", "al-", "ar-", "pl-", " 12", "٣", "AL-x"])
def test_what_is_no_id_of_ours_is_of_no_kind(given: str) -> None:
    assert ids.kind_of(given) is None


@pytest.mark.parametrize("convert,given,what", [
    (ids.to_song, "al-abc", "song"), (ids.to_song, "pl-1", "song"), (ids.to_song, "", "song"), (ids.to_song, "٣", "song"), (ids.to_song, "1a", "song"),
    (ids.to_album, "12", "album"), (ids.to_album, "ar-QQ", "album"), (ids.to_album, "al-", "album"), (ids.to_album, "nope", "album"),
    (ids.to_artist, "12", "artist"), (ids.to_artist, "pl-1", "artist"), (ids.to_artist, "ar-", "artist"), (ids.to_artist, "nope", "artist"),
    (ids.to_playlist, "12", "playlist"), (ids.to_playlist, "al-x", "playlist"), (ids.to_playlist, "pl-x", "playlist"), (ids.to_playlist, "pl-1x", "playlist"),
    (ids.to_playlist, "pl-٣", "playlist"), (ids.to_playlist, "pl-", "playlist"), (ids.to_playlist, "nope", "playlist"), (ids.to_playlist, "", "playlist"),
])
def test_an_id_of_the_wrong_kind_is_not_found_whatever_it_is_asked_as_and_it_is_said_what_was_asked_for(convert, given: str, what: str) -> None:
    with pytest.raises(SubsonicError) as caught:
        convert(given)
    assert (caught.value.code, caught.value.message) == (NOT_FOUND, f"The requested {what} was not found")


@pytest.mark.parametrize("given", ["ar-!!!!", "ar-_w", "ar-/w==", "ar-/", "ar-é", "ar-QQ~", "ar-QQ=~", "ar-Q", "ar-Q Q"])
def test_an_artist_id_that_is_not_a_name_in_base_64_is_not_found(given: str) -> None:
    with pytest.raises(SubsonicError) as caught:
        ids.to_artist(given)
    assert (caught.value.code, caught.value.message) == (NOT_FOUND, "The requested artist was not found")


def test_an_artist_id_may_come_back_with_the_padding_the_client_added() -> None:
    assert ids.to_artist(ids.artist_id("ab") + "==") == "ab" and ids.to_artist("ar-QQ===") == "A"


# ---------------------------------------------------------------------------------------------------------------------------------- errors


def test_an_error_is_its_words_when_it_is_written_out() -> None:
    assert str(SubsonicError(70, "gone")) == "gone" and str(not_found("song")) == "The requested song was not found"


def test_the_errors_that_are_made_in_one_place_say_the_number_and_the_words() -> None:
    assert (missing("id").code, missing("id").message) == (10, "Required parameter is missing: id")
    assert (not_found().code, not_found().message) == (70, "The requested data was not found")
    assert not_found("song").message == "The requested song was not found"


# -------------------------------------------------------------------------------------------------------------------------------- parameters


def test_the_first_value_of_a_name_is_the_one_and_all_of_them_are_there_for_the_names_that_repeat() -> None:
    params = Params([("id", "1"), ("id", "2")], [("id", "3"), ("u", "me")])
    assert params.get("id") == "1" and params.getlist("id") == ["1", "2", "3"] and params.get("u") == "me" and params.get("x") is None and params.getlist("x") == []
    assert params.names() == {"id", "u"}


def test_a_value_that_is_empty_is_a_value_that_was_not_sent() -> None:
    params = Params([("a", ""), ("a", "x"), ("b", "")])
    assert params.get("a") == "x" and params.getlist("a") == ["x"] and params.get("b") is None
    with pytest.raises(SubsonicError) as caught:
        params.require("b")
    assert caught.value.code == 10 and "b" in caught.value.message


def test_whole_numbers_are_kept_inside_the_limits_and_what_is_not_one_is_an_error() -> None:
    params = Params([("n", "7"), ("big", "100000"), ("small", "-5"), ("bad", "x"), ("float", "1.5")])
    assert params.integer("n") == 7 and params.integer("x") is None and params.integer("x", 3) == 3
    assert params.integer("big", high=500) == 500 and params.integer("small", low=0) == 0 and params.integer("n", low=1, high=9) == 7
    for name in ("bad", "float"):
        with pytest.raises(SubsonicError) as caught:
            params.integer(name)
        assert caught.value.code == 0 and name in caught.value.message


@pytest.mark.parametrize("text,expected", [("true", True), ("TRUE", True), (" True ", True), ("1", True), ("yes", True), ("false", False), ("0", False), ("no", False), ("", False), ("maybe", False)])
def test_a_flag_is_true_when_it_says_so(text: str, expected: bool) -> None:
    assert Params([("f", text)]).boolean("f") is expected
    assert Params([]).boolean("f") is False and Params([]).boolean("f", True) is True


# --------------------------------------------------------------------------------------------------------------------------------- the route


@pytest.fixture
def rest(web: SimpleNamespace) -> Rest:
    return Rest(web.client)


def test_a_call_is_answered_in_xml_unless_the_client_asks_for_something_else(rest: Rest) -> None:
    root = rest.xml("ping")
    assert tag(root) == "subsonic-response" and root.attrib["status"] == "ok" and root.attrib["version"] == "1.16.1"
    for asked in (None, "xml", "XML", "nonsense"):
        assert rest.get("ping", f=asked).headers["content-type"] == "text/xml; charset=utf-8"


def test_f_json_is_answered_in_json_and_jsonp_in_the_function_the_client_named(rest: Rest) -> None:
    response = rest.get("ping", f="json")
    assert response.headers["content-type"] == "application/json" and response.json()["subsonic-response"]["status"] == "ok"
    response = rest.get("ping", f="jsonp", callback="handle")
    assert response.headers["content-type"] == "text/javascript; charset=utf-8" and response.text.startswith("handle({") and response.text.endswith("})")


def test_jsonp_without_a_function_name_or_with_one_that_is_not_a_name_fails_as_json(rest: Rest) -> None:
    response = rest.get("ping", f="jsonp")
    answer = response.json()["subsonic-response"]
    assert response.headers["content-type"] == "application/json" and answer["status"] == "failed"
    assert (answer["error"]["code"], answer["error"]["message"]) == (10, "Required parameter is missing: callback")
    response = rest.get("ping", f="jsonp", callback="a(b)")
    assert response.headers["content-type"] == "application/json"
    assert (response.json()["subsonic-response"]["error"]["code"], response.json()["subsonic-response"]["error"]["message"]) == (0, "callback is not a function name")


def test_a_failure_is_still_http_200_and_a_page_that_is_never_kept(rest: Rest) -> None:
    response = rest.get("getSong", f="json", id="999999")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["subsonic-response"]["error"]["code"] == 70
    assert rest.get("ping").headers["cache-control"] == "no-store"


def test_the_older_clients_write_every_call_with_view_at_the_end(rest: Rest) -> None:
    assert rest.client.get("/rest/ping.view", params=rest.params(f="json")).json()["subsonic-response"]["status"] == "ok"
    assert rest.client.get("/rest/getLicense.view", params=rest.params(f="json")).json()["subsonic-response"]["license"]["valid"] is True


def test_a_call_this_server_does_not_do_is_a_failure_that_says_which(rest: Rest) -> None:
    assert rest.fails("savePlayQueue", 0) == "This server does not do savePlayQueue"


def test_the_names_of_calls_are_not_looked_up_in_any_other_case(rest: Rest) -> None:
    rest.fails("PING", 0)
    rest.fails("ping.vie", 0)
    assert rest.client.get("/rest/").status_code == 404


def test_a_post_with_the_parameters_in_its_form_is_answered_like_a_get(rest: Rest) -> None:
    response = rest.client.post("/rest/ping", data=rest.params(f="json"))
    assert response.json()["subsonic-response"]["status"] == "ok"
    answer = rest.client.post("/rest/getSong", data={**rest.params(f="json"), "id": "999999"}).json()["subsonic-response"]
    assert answer["error"]["code"] == 70


def test_the_form_and_the_query_both_count_and_the_query_comes_first(rest: Rest) -> None:
    response = rest.client.post("/rest/ping", params={"f": "json"}, data={"v": "1.16.1", "c": "tests", "f": "xml"})
    assert response.headers["content-type"] == "application/json"
    response = rest.client.post("/rest/getAlbumList2", params={**rest.params(f="json"), "type": "alphabeticalByName", "size": "1"}, data={"size": "3"})
    assert len(response.json()["subsonic-response"]["albumList2"]["album"]) == 1


@pytest.mark.parametrize("kind,body", [
    ("application/x-www-form-urlencoded", b"\xff\xfe not a form"),
    ("application/json", b"{}"),
    ("multipart/form-data; boundary=x", b"this is not multipart"),
    ("multipart/form-data", b"no boundary at all"),
])
def test_a_post_that_is_not_a_form_is_read_as_one_with_nothing_in_it(rest: Rest, kind: str, body: bytes) -> None:
    response = rest.client.post("/rest/ping", params=rest.params(f="json"), content=body, headers={"content-type": kind})
    assert response.status_code == 200 and response.json()["subsonic-response"]["status"] == "ok"


def test_head_is_answered_like_get_without_the_body(rest: Rest) -> None:
    response = rest.client.head("/rest/ping", params=rest.params(f="json"))
    assert response.status_code == 200 and response.content == b""


def test_the_json_and_the_xml_of_a_call_say_the_same(rest: Rest) -> None:
    for name, extra in (("getMusicFolders", {}), ("getUser", {"username": "tester"}), ("getAlbumList2", {"type": "alphabeticalByName", "size": 4}), ("getGenres", {})):
        in_json = rest.call(name, **extra)
        in_xml = canonical_xml(rest.xml(name, **extra))
        assert canonical_json(in_json) == in_xml, name
