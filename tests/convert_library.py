"""A folder of real song files, the rows that describe them, and a Windows of the stand-in's kind, for the Convert tests."""

from __future__ import annotations

import io
import json
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from musictoolkit.discs import helper as disc_helper
from tests.fake_disc_helper import audio, fake_song, set_control

FAKE = Path(__file__).parent / "fake_disc_helper.py"


class Library:
    """A folder of song files and the rows (as the tracks table has them) that describe them."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.rows: list[dict[str, Any]] = []
        folder.mkdir(parents=True, exist_ok=True)

    def add(self, name: str, samples: np.ndarray | None = None, rate: int = 44100, fmt: str = "FLAC", subtype: str = "PCM_16", **tags: Any) -> dict[str, Any]:
        path = self.folder / name
        if samples is not None:
            sf.write(str(path), samples, rate, format=fmt, subtype=subtype, **({"bitrate_mode": "CONSTANT", "compression_level": 0.19} if fmt == "MP3" else {}))
        row = {
            "id": len(self.rows) + 1, "file_path": str(path), "title": tags.pop("title", Path(name).stem), "artist": tags.pop("artist", "Aurora Vale"),
            "album_artist": tags.pop("album_artist", "Aurora Vale"), "album": tags.pop("album", "Northern Lights"), "track_number": tags.pop("track_number", len(self.rows) + 1),
            "disc_number": 1, "year": 2019, **tags,
        }
        self.rows.append(row)
        return row

    def windows_song(self, name: str, seed: int, sectors: int, *, kind: bytes = b"FAKEAAC1", rate: int = 44100, channels: int = 2, **tags: Any) -> dict[str, Any]:
        """A song the audio library cannot read and the stand-in's Windows can: eight bytes saying what it is, then the sound as a plain WAV file."""
        out = io.BytesIO()
        with wave.open(out, "wb") as sound:
            sound.setnchannels(channels)
            sound.setsampwidth(2)
            sound.setframerate(rate)
            sound.writeframes(audio(seed, 0, sectors))
        fake_song(self.folder / name, out.getvalue(), kind)
        return self.add(name, None, **tags)


def stand_in_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **control: Any) -> Path:
    """Point the disc helper at the stand-in (which decodes its own kind of song, and can be told to be slow, to refuse and so on). Returns its folder."""
    world = tmp_path / "drives"
    world.mkdir(exist_ok=True)
    if control:
        set_control(world, **control)
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(world)]))
    disc_helper.forget_listing()
    return world
