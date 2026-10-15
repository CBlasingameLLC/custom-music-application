"""Tags and a cover picture for a ripped song, written with mutagen's full interface (the tag editor's "easy" interface has no pictures and
no MusicBrainz identifiers). FLAC gets Vorbis comments and a picture block; MP3 gets ID3v2.3, which every player and device reads."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSSE, TXXX, UFID, ID3NoHeaderError
from PIL import Image

from musictoolkit import __version__

ENCODER = f"Music Toolkit {__version__}"
UTF16 = 1  # ID3v2.3 can hold Latin-1 or UTF-16; UTF-8 is only in v2.4


class TagError(Exception):
    """The tags could not be written; the message says why, in words for the person."""

    for_the_person = True

    def __init__(self, message: str, busy: bool = False) -> None:
        super().__init__(message)
        self.busy = busy  # the file is in use by another program for the moment: trying again soon may work


@dataclass
class SongTags:
    title: str
    artist: str
    album: str
    album_artist: str
    track_number: int
    track_total: int
    disc_number: int = 1
    disc_total: int = 1
    year: int | None = None
    date: str = ""  # the full release date when known ("2000-05-02"); else the year is used
    genre: str = ""
    recording_id: str | None = None  # MusicBrainz ids
    release_id: str | None = None
    release_group_id: str | None = None
    artist_ids: list[str] = field(default_factory=list)
    barcode: str = ""
    label: str = ""

    def when(self) -> str:
        return self.date or (f"{self.year:04d}" if self.year else "")


def _text(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch >= " " or ch == "\t").strip()


def _size_of(picture: bytes) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(picture)) as image:
            return image.size
    except Exception:
        return (0, 0)


def write_tags(path: Path, fmt: str, tags: SongTags, cover: bytes | None = None) -> None:
    """Write `tags` (and `cover`, a JPEG) into the finished file at `path`, replacing whatever tags it had."""
    try:
        if fmt == "flac":
            _flac(Path(path), tags, cover)
        elif fmt == "mp3":
            _mp3(Path(path), tags, cover)
        else:
            raise ValueError(f"cannot tag {fmt} files")
    except PermissionError:
        raise TagError("The file is read-only or in use by another program.", busy=True) from None
    except OSError as exc:
        raise TagError(f"The tags could not be written ({exc.strerror or exc}).") from None
    except MutagenError as exc:  # mutagen wraps the OSError of a file it cannot open
        if exc.args and isinstance(exc.args[0], PermissionError):
            raise TagError("The file is read-only or in use by another program.", busy=True) from None
        raise TagError(f"The tags could not be written ({exc}).") from None


def _flac(path: Path, tags: SongTags, cover: bytes | None) -> None:
    audio = FLAC(path)
    audio.clear()
    audio.clear_pictures()
    fields = {
        "TITLE": tags.title, "ARTIST": tags.artist, "ALBUM": tags.album, "ALBUMARTIST": tags.album_artist,
        "TRACKNUMBER": tags.track_number or "", "TRACKTOTAL": tags.track_total or "",
        "DISCNUMBER": tags.disc_number or "", "DISCTOTAL": tags.disc_total or "",
        "DATE": tags.when(), "GENRE": tags.genre, "BARCODE": tags.barcode, "LABEL": tags.label,
        "MUSICBRAINZ_TRACKID": tags.recording_id, "MUSICBRAINZ_ALBUMID": tags.release_id,
        "MUSICBRAINZ_RELEASEGROUPID": tags.release_group_id, "ENCODER": ENCODER,
    }
    for key, value in fields.items():
        if _text(value):
            audio[key] = [_text(value)]
    ids = [_text(i) for i in tags.artist_ids if _text(i)]
    if ids:
        audio["MUSICBRAINZ_ARTISTID"] = ids
        audio["MUSICBRAINZ_ALBUMARTISTID"] = ids
    if cover:
        picture = Picture()
        picture.type = 3  # front cover
        picture.mime = "image/jpeg"
        picture.desc = "Front cover"
        picture.data = cover
        picture.width, picture.height = _size_of(cover)
        picture.depth = 24
        audio.add_picture(picture)
    audio.save()


def _mp3(path: Path, tags: SongTags, cover: bytes | None) -> None:
    try:
        frames = ID3(path)
        frames.delete(path)  # whatever tags there were are replaced
    except ID3NoHeaderError:
        pass
    frames = ID3()
    frames.add(TIT2(encoding=UTF16, text=[_text(tags.title)]))
    frames.add(TPE1(encoding=UTF16, text=[_text(tags.artist)]))
    frames.add(TALB(encoding=UTF16, text=[_text(tags.album)]))
    if _text(tags.album_artist):
        frames.add(TPE2(encoding=UTF16, text=[_text(tags.album_artist)]))
    if tags.track_number:
        frames.add(TRCK(encoding=UTF16, text=[f"{tags.track_number}/{tags.track_total}" if tags.track_total else str(tags.track_number)]))
    if tags.disc_number:
        frames.add(TPOS(encoding=UTF16, text=[f"{tags.disc_number}/{tags.disc_total}" if tags.disc_total > 1 else str(tags.disc_number)]))
    if tags.when():
        frames.add(TDRC(encoding=UTF16, text=[tags.when()]))
    if _text(tags.genre):
        frames.add(TCON(encoding=UTF16, text=[_text(tags.genre)]))
    extras = {
        "MusicBrainz Album Id": [tags.release_id], "MusicBrainz Release Group Id": [tags.release_group_id],
        "MusicBrainz Artist Id": tags.artist_ids, "MusicBrainz Album Artist Id": tags.artist_ids,
        "BARCODE": [tags.barcode], "LABEL": [tags.label],
    }
    for name, values in extras.items():
        values = [_text(v) for v in values if _text(v)]
        if values:
            frames.add(TXXX(encoding=UTF16, desc=name, text=values))
    if _text(tags.recording_id):
        frames.add(UFID(owner="http://musicbrainz.org", data=_text(tags.recording_id).encode("ascii", "ignore")))
    frames.add(TSSE(encoding=UTF16, text=[ENCODER]))
    if cover:
        frames.add(APIC(encoding=UTF16, mime="image/jpeg", type=3, desc="Front cover", data=cover))
    frames.update_to_v23()  # saving as v2.3 does not convert by itself: without this the year stays in a v2.4-only frame that older players do not read
    frames.save(path, v2_version=3)
