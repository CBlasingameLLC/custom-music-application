from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

import tomli_w

from musictoolkit import __version__


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
    app_version: str = __version__


@dataclass
class ListenBrainzConfig:
    enabled: bool = True
    username: str = ""
    user_token: str = ""
    scrobble: bool = True  # submit plays from the in-app player as they happen
    now_playing: bool = False  # also tell ListenBrainz what is playing right now (shown for a few minutes, never kept)


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
class AppConfig:
    rescan_on_launch: bool = True
    lyrics_lrclib: bool = False  # look up lyrics on lrclib.net when a file has none
    release_radar: bool = False  # look for new releases by artists you play (ListenBrainz, MusicBrainz) about once a day
    auto_update: bool = True  # the desktop app checks GitHub Releases for new versions and installs them


@dataclass
class Config:
    library: LibraryConfig = field(default_factory=LibraryConfig)
    musicbrainz: MusicBrainzConfig = field(default_factory=MusicBrainzConfig)
    listenbrainz: ListenBrainzConfig = field(default_factory=ListenBrainzConfig)
    lastfm: LastFmConfig = field(default_factory=LastFmConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    app: AppConfig = field(default_factory=AppConfig)


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


# The example config used to ship this fake folder; configs bootstrapped from it still contain it.
PLACEHOLDER_ROOTS = {"/path/to/your/mp3s"}

T = TypeVar("T")


def _section(cls: type[T], raw: dict[str, Any]) -> T:
    """Build a config section, ignoring keys this version doesn't know about
    (a config written by a newer or older release must still load)."""
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    return cls(**{key: value for key, value in raw.items() if key in known})  # type: ignore[call-arg]


def _anchor_to(base_dir: Path, value: str) -> str:
    """Relative paths in a config file mean "relative to this file", not "to
    whatever the process's working directory happens to be" — a packaged app
    launched from a shortcut has an unpredictable one, and the bootstrapped
    config ships with relative defaults."""
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else base_dir / path)


def load_config(path: Path | str | None = None) -> Config:
    """Load config.toml, falling back to all-defaults if it doesn't exist.

    Pass an explicit path to load exactly that file — every test does this,
    and is completely unaffected by resolve_config_path()/bootstrap_if_missing()
    above, which only run when the caller (cli.py's main callback) resolves
    the path itself and passes None here to mean "use the default resolution."

    A relative `database.path` or `logging.dir` is resolved against the
    config file's own directory.
    """
    resolved = Path(path) if path is not None else resolve_config_path(None)
    if not resolved.exists():
        return Config()

    with resolved.open("rb") as f:
        raw = tomllib.load(f)

    base_dir = resolved.absolute().parent
    database = _section(DatabaseConfig, raw.get("database", {}))
    database.path = _anchor_to(base_dir, database.path)
    logging_cfg = _section(LoggingConfig, raw.get("logging", {}))
    logging_cfg.dir = _anchor_to(base_dir, logging_cfg.dir)

    library = _section(LibraryConfig, raw.get("library", {}))
    library.roots = [root for root in library.roots if root not in PLACEHOLDER_ROOTS]

    return Config(
        library=library,
        musicbrainz=_section(MusicBrainzConfig, raw.get("musicbrainz", {})),
        listenbrainz=_section(ListenBrainzConfig, raw.get("listenbrainz", {})),
        lastfm=_section(LastFmConfig, raw.get("lastfm", {})),
        sync=_section(SyncConfig, raw.get("sync", {})),
        database=database,
        logging=logging_cfg,
        app=_section(AppConfig, raw.get("app", {})),
    )


def save_config(path: Path | str, config: Config) -> None:
    """Write the config as TOML, atomically (a crash mid-write must not leave
    the user without a config). Comments in a hand-edited file are not kept."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(tomli_w.dumps(asdict(config)).encode("utf-8"))
    os.replace(temp, path)
