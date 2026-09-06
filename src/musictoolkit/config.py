from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_data_dir() -> Path:
    """Per-user location for config/db/logs when nothing else is specified —
    mirrors the ~/.dial/ convention from the sibling Dial project (avoids
    Documents/'s OneDrive sync-corruption risk and %APPDATA%'s MSIX-container
    snapshot problem)."""
    return Path.home() / ".musictoolkit"


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
    username: str = ""
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
    path: str = field(default_factory=lambda: str(default_data_dir() / "data" / "library.db"))


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = field(default_factory=lambda: str(default_data_dir() / "logs"))


@dataclass
class Config:
    library: LibraryConfig = field(default_factory=LibraryConfig)
    musicbrainz: MusicBrainzConfig = field(default_factory=MusicBrainzConfig)
    listenbrainz: ListenBrainzConfig = field(default_factory=ListenBrainzConfig)
    lastfm: LastFmConfig = field(default_factory=LastFmConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def resolve_config_path(explicit: str | Path | None) -> Path:
    """Precedence: an explicit path always wins; otherwise prefer a
    config.toml in the current directory (keeps running `mtk` from a repo
    checkout working exactly as before); otherwise fall back to the
    per-user default location, for a packaged app launched from a shortcut
    with an unpredictable working directory."""
    if explicit is not None:
        return Path(explicit)
    cwd_config = Path("config.toml")
    if cwd_config.exists():
        return cwd_config
    return default_data_dir() / "config.toml"


def bootstrap_if_missing(path: Path, example_path: Path | None = None) -> None:
    """First-run convenience: if the resolved config location has nothing
    there yet, seed it from config.example.toml so there's a real, findable,
    editable file rather than silence. Never touches a path that already
    exists, and never runs for a path the caller passed in explicitly to
    load_config() directly — only cli.py's default-resolution path calls
    this."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if example_path and example_path.exists():
        shutil.copy(example_path, path)


def load_config(path: Path | str | None = None) -> Config:
    """Load config.toml, falling back to all-defaults if it doesn't exist.

    Pass an explicit path to load exactly that file — every test does this,
    and is completely unaffected by resolve_config_path()/bootstrap_if_missing()
    above, which only run when the caller (cli.py's main callback) resolves
    the path itself and passes None here to mean "use the default resolution."
    """
    resolved = Path(path) if path is not None else resolve_config_path(None)
    if not resolved.exists():
        return Config()

    with resolved.open("rb") as f:
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
