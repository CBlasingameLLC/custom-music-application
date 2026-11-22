"""Reading the tags of a song file, whatever kind of file it is, into one set of fields (`SongTags`) that `discs.tagging` can write into another kind.

MP3, WAV and AIFF files keep ID3 frames; FLAC, Ogg Vorbis and Opus keep Vorbis comments; M4A keeps atoms; WMA keeps ASF attributes. A field a
file does not have comes out empty (a number as 0), and a file that cannot be read at all comes out with nothing: converting a song never fails for
want of tags.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import mutagen

from musictoolkit.discs.tagging import REPLAYGAIN, SongTags

JOIN = "; "  # several artists in one field


def _pair(value: object) -> tuple[int, int]:
    """"3/12" is track 3 of 12; "3" is track 3; anything else is nothing."""
    match = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", str(value or ""))
    return (int(match.group(1)), int(match.group(2) or 0)) if match else (0, 0)


def _year(value: object) -> int | None:
    match = re.match(r"\s*(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def _strings(values: Any) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(getattr(value, "value", value))
        if text.strip():
            out.append(text.strip())
    return out


def _joined(values: Any) -> str:
    return JOIN.join(_strings(values))


def _blank() -> SongTags:
    return SongTags("", "", "", "", 0, 0, disc_number=0, disc_total=0)


def _from_id3(frames: Any) -> SongTags:
    tags = _blank()
    if frames is None:
        return tags

    def text(frame_id: str) -> list[str]:
        frame = frames.get(frame_id)
        return _strings(frame.text) if frame is not None else []

    extras = {str(f.desc).lower(): _strings(f.text) for f in frames.getall("TXXX")}
    tags.title, tags.artist, tags.album, tags.album_artist = _joined(text("TIT2")), _joined(text("TPE1")), _joined(text("TALB")), _joined(text("TPE2"))
    tags.track_number, tags.track_total = _pair((text("TRCK") or [""])[0])
    tags.disc_number, tags.disc_total = _pair((text("TPOS") or [""])[0])
    tags.date = (text("TDRC") or [""])[0]
    tags.year = _year(tags.date) or _year((text("TYER") or [""])[0])
    tags.genre, tags.composer = _joined(text("TCON")), _joined(text("TCOM"))
    comments = frames.getall("COMM")
    tags.comment = "\n".join(_strings(comments[0].text)) if comments else ""
    lyrics = frames.getall("USLT")
    tags.lyrics = str(lyrics[0].text).strip() if lyrics else ""
    ufid = frames.get("UFID:http://musicbrainz.org")
    tags.recording_id = ufid.data.decode("ascii", "ignore") if ufid is not None and ufid.data else None
    tags.release_id = (extras.get("musicbrainz album id") or [None])[0]
    tags.release_group_id = (extras.get("musicbrainz release group id") or [None])[0]
    tags.artist_ids = extras.get("musicbrainz artist id", [])
    tags.barcode = (extras.get("barcode") or [""])[0]
    tags.label = (extras.get("label") or text("TPUB") or [""])[0]
    tags.replaygain = {key: extras[name.lower()][0] for key, name in REPLAYGAIN.items() if extras.get(name.lower())}
    return tags


def _from_vorbis(comments: Any) -> SongTags:
    tags = _blank()
    if comments is None:
        return tags

    def get(*keys: str) -> list[str]:
        for key in keys:
            values = _strings(comments.get(key))
            if values:
                return values
        return []

    def one(*keys: str) -> str:
        return (get(*keys) or [""])[0]

    tags.title, tags.artist, tags.album = _joined(get("title")), _joined(get("artist")), _joined(get("album"))
    tags.album_artist = _joined(get("albumartist", "album artist"))
    tags.track_number, tags.track_total = _pair(one("tracknumber"))
    tags.track_total = tags.track_total or _pair(one("tracktotal", "totaltracks"))[0]
    tags.disc_number, tags.disc_total = _pair(one("discnumber"))
    tags.disc_total = tags.disc_total or _pair(one("disctotal", "totaldiscs"))[0]
    tags.date = one("date", "year")
    tags.year = _year(tags.date)
    tags.genre, tags.composer = _joined(get("genre")), _joined(get("composer"))
    tags.comment, tags.lyrics = "\n".join(get("comment", "description")), "\n".join(get("lyrics", "unsyncedlyrics"))
    tags.recording_id = one("musicbrainz_trackid") or None
    tags.release_id = one("musicbrainz_albumid") or None
    tags.release_group_id = one("musicbrainz_releasegroupid") or None
    tags.artist_ids = get("musicbrainz_artistid")
    tags.barcode, tags.label = one("barcode"), one("label", "organization", "publisher")
    tags.replaygain = {key: one(name) for key, name in REPLAYGAIN.items() if one(name)}
    return tags


def _from_mp4(atoms: Any) -> SongTags:
    tags = _blank()
    if atoms is None:
        return tags
    free = {str(key).split(":")[-1].lower(): _strings(value) for key, value in atoms.items() if str(key).startswith("----")}

    def get(key: str) -> list[str]:
        return _strings(atoms.get(key))

    def pair(key: str) -> tuple[int, int]:
        value = (atoms.get(key) or [(0, 0)])[0]
        return (int(value[0] or 0), int(value[1] or 0)) if isinstance(value, tuple) else _pair(value)

    tags.title, tags.artist, tags.album, tags.album_artist = _joined(get("\xa9nam")), _joined(get("\xa9ART")), _joined(get("\xa9alb")), _joined(get("aART"))
    tags.track_number, tags.track_total = pair("trkn")
    tags.disc_number, tags.disc_total = pair("disk")
    tags.date = (get("\xa9day") or [""])[0]
    tags.year = _year(tags.date)
    tags.genre, tags.composer = _joined(get("\xa9gen")), _joined(get("\xa9wrt"))
    tags.comment, tags.lyrics = "\n".join(get("\xa9cmt")), "\n".join(get("\xa9lyr"))
    tags.recording_id = (free.get("musicbrainz track id") or [None])[0]
    tags.release_id = (free.get("musicbrainz album id") or [None])[0]
    tags.release_group_id = (free.get("musicbrainz release group id") or [None])[0]
    tags.artist_ids = free.get("musicbrainz artist id", [])
    tags.barcode, tags.label = (free.get("barcode") or [""])[0], (free.get("label") or [""])[0]
    tags.replaygain = {key: free[name.lower()][0] for key, name in REPLAYGAIN.items() if free.get(name.lower())}
    return tags


def _from_asf(attributes: Any) -> SongTags:
    tags = _blank()
    if attributes is None:
        return tags

    def get(*keys: str) -> list[str]:
        for key in keys:
            values = _strings(attributes.get(key))
            if values:
                return values
        return []

    tags.title, tags.artist, tags.album = _joined(get("Title")), _joined(get("Author", "WM/Author")), _joined(get("WM/AlbumTitle"))
    tags.album_artist = _joined(get("WM/AlbumArtist"))
    tags.track_number, tags.track_total = _pair((get("WM/TrackNumber", "WM/Track") or [""])[0])
    tags.disc_number, tags.disc_total = _pair((get("WM/PartOfSet") or [""])[0])
    tags.date = (get("WM/Year") or [""])[0]
    tags.year = _year(tags.date)
    tags.genre, tags.composer = _joined(get("WM/Genre")), _joined(get("WM/Composer"))
    tags.comment, tags.lyrics = "\n".join(get("Description")), "\n".join(get("WM/Lyrics"))
    tags.recording_id = (get("MusicBrainz/Track Id") or [None])[0]
    tags.release_id = (get("MusicBrainz/Album Id") or [None])[0]
    tags.release_group_id = (get("MusicBrainz/Release Group Id") or [None])[0]
    tags.artist_ids = get("MusicBrainz/Artist Id")
    tags.barcode, tags.label = (get("WM/Barcode") or [""])[0], (get("WM/Publisher") or [""])[0]
    return tags


def read_tags(path: str | Path) -> SongTags:
    """Everything a song file says about itself that a converted copy should carry (the picture is `media.art.find_art`'s business)."""
    try:
        audio = mutagen.File(str(path))
    except Exception:  # a damaged tag must never stop a song from being converted
        return _blank()
    if audio is None:
        return _blank()
    try:
        kind = type(audio).__module__.rsplit(".", 1)[-1]
        if kind in ("flac", "oggvorbis", "oggopus", "oggflac", "oggspeex", "oggtheora"):
            return _from_vorbis(audio.tags)
        if kind == "mp4":
            return _from_mp4(audio.tags)
        if kind == "asf":
            return _from_asf(audio.tags)
        return _from_id3(audio.tags)  # (MP3, WAV, AIFF, and what else keeps ID3 frames)
    except Exception:
        return _blank()
