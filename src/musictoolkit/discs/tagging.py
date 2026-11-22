"""Tags and a cover picture for a song the app has written (a ripped one, a converted one), through mutagen's full interface (the tag editor's
"easy" interface has no pictures and no MusicBrainz identifiers). FLAC, Ogg Vorbis and Opus get Vorbis comments and a picture; MP3 gets ID3v2.3, which
every player and device reads, and so does a WAV file (in its ID3 chunk)."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSSE, TXXX, UFID, USLT, ID3NoHeaderError
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
from PIL import Image

from musictoolkit import __version__
from musictoolkit.media.art import sniff_mime

ENCODER = f"Music Toolkit {__version__}"
UTF16 = 1  # ID3v2.3 can hold Latin-1 or UTF-16; UTF-8 is only in v2.4
REPLAYGAIN = {"track_gain": "REPLAYGAIN_TRACK_GAIN", "track_peak": "REPLAYGAIN_TRACK_PEAK", "album_gain": "REPLAYGAIN_ALBUM_GAIN", "album_peak": "REPLAYGAIN_ALBUM_PEAK"}
FORMATS = ("flac", "mp3", "ogg", "opus", "wav")


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
    composer: str = ""
    comment: str = ""
    lyrics: str = ""
    replaygain: dict[str, str] = field(default_factory=dict)  # track_gain, track_peak, album_gain, album_peak: as the files write them ("-7.50 dB", "0.988")

    def when(self) -> str:
        return self.date or (f"{self.year:04d}" if self.year else "")


def _text(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch >= " " or ch == "\t").strip()


def _block(value: object) -> str:
    """Lyrics and comments run over several lines: those are kept (as plain line feeds), the rest is cleaned as everything else is."""
    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join("".join(ch for ch in line if ch >= " " or ch == "\t").rstrip() for line in lines).strip()


def _size_of(picture: bytes) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(picture)) as image:
            return image.size
    except Exception:
        return (0, 0)


def _mime(cover: bytes) -> str:
    found = sniff_mime(cover)
    return found if found.startswith("image/") else "image/jpeg"


def write_tags(path: Path, fmt: str, tags: SongTags, cover: bytes | None = None) -> None:
    """Write `tags` (and `cover`, a picture file's bytes) into the finished file at `path`, replacing whatever tags it had."""
    try:
        if fmt == "flac":
            _flac(Path(path), tags, cover)
        elif fmt == "mp3":
            _mp3(Path(path), tags, cover)
        elif fmt in ("ogg", "opus"):
            _ogg(Path(path), tags, cover, OggOpus if fmt == "opus" else OggVorbis)
        elif fmt == "wav":
            _wav(Path(path), tags, cover)
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


def _vorbis_fields(tags: SongTags) -> dict[str, list[str]]:
    """The Vorbis comments a song has (FLAC, Ogg Vorbis and Opus all use them), the ones with something to say."""
    fields = {
        "TITLE": tags.title, "ARTIST": tags.artist, "ALBUM": tags.album, "ALBUMARTIST": tags.album_artist,
        "TRACKNUMBER": tags.track_number or "", "TRACKTOTAL": tags.track_total or "",
        "DISCNUMBER": tags.disc_number or "", "DISCTOTAL": tags.disc_total or "",
        "DATE": tags.when(), "GENRE": tags.genre, "BARCODE": tags.barcode, "LABEL": tags.label,
        "COMPOSER": tags.composer,
        "MUSICBRAINZ_TRACKID": tags.recording_id, "MUSICBRAINZ_ALBUMID": tags.release_id,
        "MUSICBRAINZ_RELEASEGROUPID": tags.release_group_id, "ENCODER": ENCODER,
    }
    for key, name in REPLAYGAIN.items():
        fields[name] = tags.replaygain.get(key, "")
    out = {key: [_text(value)] for key, value in fields.items() if _text(value)}
    for key, value in (("COMMENT", tags.comment), ("LYRICS", tags.lyrics)):
        if _block(value):
            out[key] = [_block(value)]
    ids = [_text(i) for i in tags.artist_ids if _text(i)]
    if ids:
        out["MUSICBRAINZ_ARTISTID"] = ids
        out["MUSICBRAINZ_ALBUMARTISTID"] = ids
    return out


def _picture(cover: bytes) -> Picture:
    picture = Picture()
    picture.type = 3  # front cover
    picture.mime = _mime(cover)
    picture.desc = "Front cover"
    picture.data = cover
    picture.width, picture.height = _size_of(cover)
    picture.depth = 24
    return picture


def _flac(path: Path, tags: SongTags, cover: bytes | None) -> None:
    audio = FLAC(path)
    audio.clear()
    audio.clear_pictures()
    for key, values in _vorbis_fields(tags).items():
        audio[key] = values
    if cover:
        audio.add_picture(_picture(cover))
    audio.save()


def _ogg(path: Path, tags: SongTags, cover: bytes | None, kind: type) -> None:
    audio = kind(path)
    audio.clear()
    for key, values in _vorbis_fields(tags).items():
        audio[key] = values
    if cover:  # (an Ogg file has no picture block: a FLAC one is written into a comment, the way Vorbis players look for it)
        audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(_picture(cover).write()).decode("ascii")]
    audio.save()


def _id3_frames(tags: SongTags, cover: bytes | None) -> ID3:
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
    if _text(tags.composer):
        frames.add(TCOM(encoding=UTF16, text=[_text(tags.composer)]))
    if _block(tags.comment):
        frames.add(COMM(encoding=UTF16, lang="eng", desc="", text=[_block(tags.comment)]))
    if _block(tags.lyrics):
        frames.add(USLT(encoding=UTF16, lang="eng", desc="", text=_block(tags.lyrics)))
    extras = {
        "MusicBrainz Album Id": [tags.release_id], "MusicBrainz Release Group Id": [tags.release_group_id],
        "MusicBrainz Artist Id": tags.artist_ids, "MusicBrainz Album Artist Id": tags.artist_ids,
        "BARCODE": [tags.barcode], "LABEL": [tags.label],
    }
    for key, name in REPLAYGAIN.items():
        extras[name] = [tags.replaygain.get(key, "")]
    for name, values in extras.items():
        values = [_text(v) for v in values if _text(v)]
        if values:
            frames.add(TXXX(encoding=UTF16, desc=name, text=values))
    if _text(tags.recording_id):
        frames.add(UFID(owner="http://musicbrainz.org", data=_text(tags.recording_id).encode("ascii", "ignore")))
    frames.add(TSSE(encoding=UTF16, text=[ENCODER]))
    if cover:
        frames.add(APIC(encoding=UTF16, mime=_mime(cover), type=3, desc="Front cover", data=cover))
    frames.update_to_v23()  # saving as v2.3 does not convert by itself: without this the year stays in a v2.4-only frame that older players do not read
    return frames


def _mp3(path: Path, tags: SongTags, cover: bytes | None) -> None:
    try:
        old = ID3(path)
        old.delete(path)  # whatever tags there were are replaced
    except ID3NoHeaderError:
        pass
    _id3_frames(tags, cover).save(path, v2_version=3)


def _wav(path: Path, tags: SongTags, cover: bytes | None) -> None:
    audio = WAVE(path)
    if audio.tags is not None:
        audio.delete()  # (whatever tags there were are replaced)
        audio = WAVE(path)
    audio.add_tags()
    for frame in _id3_frames(tags, cover).values():
        audio.tags.add(frame)
    audio.save(v2_version=3)
