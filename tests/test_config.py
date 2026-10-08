from pathlib import Path

from musictoolkit.config import (
    Config,
    bootstrap_if_missing,
    default_data_dir,
    load_config,
    resolve_config_path,
)


def test_load_config_defaults_when_missing(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does_not_exist.toml")
    assert isinstance(cfg, Config)
    assert cfg.listenbrainz.enabled is True
    assert cfg.lastfm.enabled is False
    assert cfg.database.path == str(default_data_dir() / "data" / "library.db")


def test_load_config_reads_values(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[library]
roots = ["/music"]

[musicbrainz]
contact = "test@example.com"

[listenbrainz]
user_token = "abc123"

[lastfm]
enabled = true
api_key = "key"
""",
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.library.roots == ["/music"]
    assert cfg.musicbrainz.contact == "test@example.com"
    assert cfg.listenbrainz.user_token == "abc123"
    assert cfg.lastfm.enabled is True
    assert cfg.lastfm.api_key == "key"


def test_relative_db_and_log_paths_anchor_to_the_config_dir_not_cwd(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (config_dir / "config.toml").write_text(
        '[database]\npath = "./data/library.db"\n\n[logging]\ndir = "./logs"\n', encoding="utf-8"
    )
    monkeypatch.chdir(elsewhere)

    cfg = load_config(config_dir / "config.toml")

    assert cfg.database.path == str(config_dir / "data" / "library.db")
    assert cfg.logging.dir == str(config_dir / "logs")


def test_relative_paths_in_a_cwd_config_resolve_against_that_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config.toml").write_text('[database]\npath = "./data/library.db"\n', encoding="utf-8")

    cfg = load_config(Path("config.toml"))

    assert cfg.database.path == str(Path.cwd() / "data" / "library.db")


def test_absolute_db_path_is_left_alone(tmp_path: Path) -> None:
    absolute = tmp_path / "somewhere" / "my.db"
    (tmp_path / "config.toml").write_text(f'[database]\npath = "{absolute.as_posix()}"\n', encoding="utf-8")

    assert load_config(tmp_path / "config.toml").database.path == str(absolute)


def test_omitted_db_and_log_keys_keep_the_per_user_defaults(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[library]\nroots = []\n", encoding="utf-8")

    cfg = load_config(tmp_path / "config.toml")

    assert cfg.database.path == str(default_data_dir() / "data" / "library.db")
    assert cfg.logging.dir == str(default_data_dir() / "logs")


def test_bootstrapped_example_config_keeps_its_data_beside_itself(tmp_path: Path, monkeypatch) -> None:
    """Regression: the installed app copies config.example.toml to the per-user
    folder on first run. Its relative db/log paths used to resolve against the
    launch directory (the install dir), so the first page load failed."""
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    target = tmp_path / "home" / ".musictoolkit" / "config.toml"
    bootstrap_if_missing(target, example_path=example)
    monkeypatch.chdir(tmp_path)  # like a Start Menu shortcut: not the config's folder

    cfg = load_config(target)

    assert Path(cfg.database.path) == target.parent / "data" / "library.db"
    assert Path(cfg.logging.dir) == target.parent / "logs"


def test_resolve_config_path_explicit_always_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "somewhere" / "custom.toml"
    assert resolve_config_path(explicit) == explicit
    assert resolve_config_path(str(explicit)) == explicit


def test_resolve_config_path_prefers_cwd_config_over_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    assert resolve_config_path(None) == Path("config.toml")


def test_resolve_config_path_falls_back_to_default_data_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no config.toml here
    assert resolve_config_path(None) == default_data_dir() / "config.toml"


def test_bootstrap_if_missing_seeds_from_example(tmp_path: Path) -> None:
    example = tmp_path / "config.example.toml"
    example.write_text("[library]\nroots = []\n", encoding="utf-8")
    target = tmp_path / "nested" / "config.toml"

    bootstrap_if_missing(target, example_path=example)

    assert target.exists()
    assert target.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")


def test_bootstrap_if_missing_never_overwrites_existing_file(tmp_path: Path) -> None:
    example = tmp_path / "config.example.toml"
    example.write_text("[library]\nroots = []\n", encoding="utf-8")
    target = tmp_path / "config.toml"
    target.write_text("my real content", encoding="utf-8")

    bootstrap_if_missing(target, example_path=example)

    assert target.read_text(encoding="utf-8") == "my real content"


def test_bootstrap_if_missing_handles_no_example_gracefully(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    bootstrap_if_missing(target, example_path=None)
    assert not target.exists()
    assert target.parent.exists()
