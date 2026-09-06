from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LibraryConfig:
    roots: list[str] = field(default_factory=list)
    canonical_scheme: str = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


@dataclass
class MusicBrainzConfig:
    contact: str = ""
    app_name: str = "custom-music-application"
    app_version: str = "0.1.0"


@dataclass
class ListenBrainzConfig:
    enabled: bool = True
    user_token: str = ""


@dataclass
class LastFmConfig:
    enabled: bool = False
    api_key: str = ""
    api_secret: str = ""


@dataclass
class SyncConfig:
    device_scheme: str = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


@dataclass
class DatabaseConfig:
    path: str = "./data/library.db"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = "./logs"


@dataclass
class Config:
    library: LibraryConfig = field(default_factory=LibraryConfig)
    musicbrainz: MusicBrainzConfig = field(default_factory=MusicBrainzConfig)
    listenbrainz: ListenBrainzConfig = field(default_factory=ListenBrainzConfig)
    lastfm: LastFmConfig = field(default_factory=LastFmConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(path: Path | str = "config.toml") -> Config:
    """Load config.toml, falling back to all-defaults if it doesn't exist yet."""
    path = Path(path)
    if not path.exists():
        return Config()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    return Config(
        library=LibraryConfig(**raw.get("library", {})),
        musicbrainz=MusicBrainzConfig(**raw.get("musicbrainz", {})),
        listenbrainz=ListenBrainzConfig(**raw.get("listenbrainz", {})),
        lastfm=LastFmConfig(**raw.get("lastfm", {})),
        sync=SyncConfig(**raw.get("sync", {})),
        database=DatabaseConfig(**raw.get("database", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
    )
