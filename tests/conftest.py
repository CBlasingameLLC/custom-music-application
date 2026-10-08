from __future__ import annotations

import io
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3
from PIL import Image

from musictoolkit.config import Config
from musictoolkit.web.app import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def jpeg_bytes(color: tuple[int, int, int] = (200, 40, 40), size: int = 300) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (size, size), color).save(out, "JPEG")
    return out.getvalue()


def make_track(directory: Path, filename: str, source: str = "complete_tags.mp3", **tags: object) -> Path:
    """A real MP3 (copied from a checked-in fixture) carrying exactly these tags.

    The default source lasts a fraction of a second; pass source="tone_12s.mp3" for a
    song that can actually be played back in a browser test."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    shutil.copy(FIXTURES_DIR / source, path)
    audio = EasyID3(path)
    audio.delete()
    for key, value in tags.items():
        audio[key] = str(value)
    audio.save()
    return path


def add_cover(path: Path, color: tuple[int, int, int] = (200, 40, 40)) -> None:
    id3 = ID3(path)
    id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover", data=jpeg_bytes(color)))
    id3.save()


def build_music_dir(root: Path, source: str = "complete_tags.mp3") -> Path:
    """9 tracks: 3 artists, 4 albums, 4 genres; only 'Northern Lights' has embedded art."""
    nl = root / "Aurora Vale" / "Northern Lights"
    for n, title in enumerate(["Glacier", "Polar Night", "Drift"], start=1):
        make_track(nl, f"{n:02d} - {title}.mp3", source, title=title, artist="Aurora Vale", albumartist="Aurora Vale",
                   album="Northern Lights", tracknumber=n, date="2019", genre="Electronic")
    for path in nl.glob("*.mp3"):
        add_cover(path)
    make_track(root / "Aurora Vale" / "Low Tide", "01 - Salt and Stone.mp3", source, title="Salt and Stone",
               artist="Aurora Vale", albumartist="Aurora Vale", album="Low Tide", tracknumber=1, date="2022",
               genre="Ambient")
    br = root / "The Fernwoods" / "Back Roads"
    make_track(br, "01 - Gravel Dust.mp3", source, title="Gravel Dust", artist="The Fernwoods",
               albumartist="The Fernwoods", album="Back Roads", tracknumber=1, date="2015", genre="Folk")
    make_track(br, "02 - Porch Light.mp3", source, title="Porch Light", artist="The Fernwoods",
               albumartist="The Fernwoods", album="Back Roads", tracknumber=2, date="2015", genre="Folk")
    sh = root / "Mara Quinn" / "Static Hearts"
    for n, title in enumerate(["Wires", "Neon Rain", "Signal Fade"], start=1):
        make_track(sh, f"{n:02d} - {title}.mp3", source, title=title, artist="Mara Quinn", albumartist="Mara Quinn",
                   album="Static Hearts", tracknumber=n, date="2021", genre="Indie Rock")
    return root


@pytest.fixture
def music_dir(tmp_path: Path) -> Path:
    return build_music_dir(tmp_path / "music")


def build_web(tmp_path: Path, music_dir: Path | None, token: str | None = None) -> SimpleNamespace:
    config = Config()
    config.database.path = str(tmp_path / "data" / "library.db")
    config.logging.dir = str(tmp_path / "logs")
    if music_dir is not None:
        config.library.roots = [str(music_dir)]
    app = create_app(
        db_path=Path(config.database.path), config_path=tmp_path / "config.toml", config=config, token=token
    )
    client = TestClient(app, raise_server_exceptions=False)
    return SimpleNamespace(app=app, client=client, ctx=app.state.ctx, config=config)


def scan(web: SimpleNamespace) -> dict:
    response = web.client.post("/api/library/scan")
    assert response.status_code == 200, response.text
    job = web.ctx.jobs.wait(response.json()["job"]["id"], timeout=60)
    assert job.status == "done", job.error
    return job.result


@pytest.fixture
def web(tmp_path: Path, music_dir: Path) -> SimpleNamespace:
    """A running app over an already-scanned library (no auth, like unit tests of any API)."""
    app = build_web(tmp_path, music_dir)
    scan(app)
    return app


@pytest.fixture
def empty_web(tmp_path: Path) -> SimpleNamespace:
    """A freshly installed app: no folders added, nothing scanned."""
    return build_web(tmp_path, None)


def ids_by_title(client: TestClient) -> dict[str, int]:
    items = client.get("/api/tracks", params={"limit": 1000}).json()["items"]
    return {item["title"]: item["id"] for item in items}
