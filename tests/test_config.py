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
