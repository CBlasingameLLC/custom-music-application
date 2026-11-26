"""The files that set a server up (the service, the example config, the install guide) must agree with each other and with the program."""

from __future__ import annotations

import configparser
from pathlib import Path

from musictoolkit import __version__
from musictoolkit import server as server_ops
from musictoolkit.config import load_config

PACKAGING = Path(__file__).resolve().parent.parent / "packaging" / "linux"


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # (the keys of a unit are case-sensitive)
    parser.read(PACKAGING / "musictoolkit.service", encoding="utf-8")
    return parser


def test_the_example_config_is_one_a_server_can_start_from() -> None:
    config = load_config(PACKAGING / "config.example.toml")
    assert config.library.roots == ["/srv/music/library"]
    assert (config.server.host, config.server.port) == ("127.0.0.1", 4533)
    assert server_ops.allowed_names(config.server.host, config.server.allowed_hosts) == ["musicpi.local"]
    assert (config.server.rescan_hours, config.server.login_days) == (6, 90)
    assert config.app.auto_update is False and config.app.rescan_on_launch is True
    assert Path(config.database.path) == PACKAGING / "data" / "library.db"  # (relative to the config file's own folder, as ever)
    assert Path(config.logging.dir) == PACKAGING / "logs"


def test_the_service_runs_the_server_as_its_own_account_on_the_data_folder_after_the_drive_is_there() -> None:
    unit = _unit()
    service = unit["Service"]
    assert service["ExecStart"] == "/opt/musictoolkit/venv/bin/mtk --config /var/lib/musictoolkit/config.toml serve"
    assert unit["Unit"]["RequiresMountsFor"] == "/srv/music"
    assert (service["User"], service["Group"], service["SupplementaryGroups"], service["UMask"]) == ("mtk", "mtk", "music", "0002")
    assert service["EnvironmentFile"] == "-/etc/musictoolkit.env"  # (the dash: the file is optional, the server then makes its own token)
    assert service["Restart"] == "on-failure"
    assert unit["Install"]["WantedBy"] == "multi-user.target"


def test_the_service_may_write_only_the_music_and_its_own_data() -> None:
    service = _unit()["Service"]
    assert (service["ProtectSystem"], service["NoNewPrivileges"], service["PrivateTmp"], service["ProtectHome"]) == ("strict", "yes", "yes", "yes")
    writable = service["ReadWritePaths"].split()
    assert writable == ["/srv/music", "/var/lib/musictoolkit"]
    config = load_config(PACKAGING / "config.example.toml")
    assert all(any(root == place or root.startswith(place + "/") for place in writable) for root in config.library.roots)  # (the library is inside what it may write)
    assert service["ExecStart"].split()[2] == "/var/lib/musictoolkit/config.toml"  # (so the data folder, which holds the config, the database and the token, is the second)


def test_the_example_environment_file_has_an_empty_token_and_says_how_to_make_one() -> None:
    lines = (PACKAGING / "musictoolkit.env.example").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if not line.startswith("#")] == ["MTK_TOKEN="]
    assert any("openssl rand -hex 32" in line for line in lines)


def test_the_install_guide_names_the_same_places_as_the_service_and_the_version_it_installs() -> None:
    guide = (PACKAGING / "README.md").read_text(encoding="utf-8")
    for place in ("/opt/musictoolkit/venv", "/var/lib/musictoolkit", "/srv/music/library", "/srv/music/inbox", "/srv/music/exports", "musictoolkit.service"):
        assert place in guide, place
    assert f"--branch v{__version__} " in guide  # (the guide installs the release it came with)
    assert "mtk --config /var/lib/musictoolkit/config.toml token --url" in guide
    assert _unit()["Service"]["ExecStart"].split()[0] in guide
