"""The calls about the server itself: is it there, is it licensed, what does it add to the protocol, and who is asking."""

from __future__ import annotations

from typing import Any

from musictoolkit.radio import store
from musictoolkit.web.routers.manage import submit_scan
from musictoolkit.web.subsonic.dispatch import Call, endpoint
from musictoolkit.web.subsonic.errors import GENERIC, SubsonicError

# What this server adds to Subsonic 1.16.1, by OpenSubsonic's names (https://opensubsonic.netlify.app/docs/extensions/).
EXTENSIONS = [
    {"name": "apiKeyAuthentication", "versions": [1]},
    {"name": "formPost", "versions": [1]},
    {"name": "songLyrics", "versions": [1]},
]


@endpoint("ping")
def ping(call: Call) -> dict[str, Any]:
    return {}


@endpoint("getLicense")
def get_license(call: Call) -> dict[str, Any]:
    """Some clients will not go on without a license that says valid; this server has none to sell."""
    return {"license": {"valid": True, "email": "", "licenseExpires": "2099-12-31T23:59:59.000Z"}}


@endpoint("getOpenSubsonicExtensions", public=True)
def get_extensions(call: Call) -> dict[str, Any]:
    return {"openSubsonicExtensions": EXTENSIONS}


@endpoint("tokenInfo")
def token_info(call: Call) -> dict[str, Any]:
    """What OpenSubsonic's `apiKeyAuthentication` adds: whose key this is (the server has one person, so it is the name the client signed in as)."""
    return {"tokenInfo": {"username": call.user}}


@endpoint("getMusicFolders")
def get_music_folders(call: Call) -> dict[str, Any]:
    """One folder, the whole library: the folders of the library are not told apart by a client of this protocol."""
    return {"musicFolders": {"musicFolder": [{"id": 1, "name": "Music"}]}}


@endpoint("getUser")
def get_user(call: Call) -> dict[str, Any]:
    name = call.params.get("username") or call.user
    return {
        "user": {
            "username": name, "scrobblingEnabled": True, "adminRole": False, "settingsRole": False, "downloadRole": True, "uploadRole": False,
            "playlistRole": True, "coverArtRole": True, "commentRole": False, "podcastRole": False, "streamRole": True, "jukeboxRole": False,
            "shareRole": False, "videoConversionRole": False, "folder": [1],
        }
    }


@endpoint("getScanStatus")
def get_scan_status(call: Call) -> dict[str, Any]:
    """Whether the library is being scanned, and how many files it has been through so far (when it is not: how many files the library has)."""
    running = [job for job in call.ctx.jobs.active() if job.kind == "scan"]
    if running:
        return {"scanStatus": {"scanning": True, "count": running[0].done}}
    with call.ctx.db() as conn:
        files = conn.execute("SELECT COUNT(*) AS n FROM tracks WHERE is_missing = 0").fetchone()["n"]
    return {"scanStatus": {"scanning": False, "count": files}}


@endpoint("startScan")
def start_scan(call: Call) -> dict[str, Any]:
    """The same scan of the music folders as the Library page starts (one at a time: asking while one is running is told it is)."""
    roots = list(call.ctx.config.library.roots)
    if not roots:
        raise SubsonicError(GENERIC, "Add a music folder first")
    submit_scan(call.ctx, roots)
    return get_scan_status(call)


@endpoint("getInternetRadioStations")
def get_internet_radio_stations(call: Call) -> dict[str, Any]:
    """The stations the person keeps (the stream is the station's own address: the client plays it itself)."""
    with call.ctx.db() as conn:
        kept = store.list_stations(conn)
    return {"internetRadioStations": {"internetRadioStation": [
        {"id": str(station["id"]), "name": station["name"], "streamUrl": station["stream_url"], "homePageUrl": station["homepage"]} for station in kept
    ]}}


# What this server has none of, said the way the protocol says "none": a client asks for these when it starts, and does not go on without an answer.
@endpoint("getBookmarks")
def get_bookmarks(call: Call) -> dict[str, Any]:
    return {"bookmarks": {}}


@endpoint("getShares")
def get_shares(call: Call) -> dict[str, Any]:
    return {"shares": {}}


@endpoint("getChatMessages")
def get_chat_messages(call: Call) -> dict[str, Any]:
    return {"chatMessages": {}}
