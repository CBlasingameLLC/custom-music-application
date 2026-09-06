from pathlib import Path

from musictoolkit.config import Config, load_config


def test_load_config_defaults_when_missing(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does_not_exist.toml")
    assert isinstance(cfg, Config)
    assert cfg.listenbrainz.enabled is True
    assert cfg.lastfm.enabled is False
    assert cfg.database.path == "./data/library.db"


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
