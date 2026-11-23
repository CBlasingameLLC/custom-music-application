"""Running the app as a server: which names it answers to, the token, the cookie, the health check, and `mtk serve` itself."""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from musictoolkit import __version__
from musictoolkit import server as server_ops
from musictoolkit.cli import app as cli_app
from musictoolkit.config import Config, ServerConfig, load_config, save_config
from musictoolkit.web.hosts import LOOPBACK_HOSTS, clean_names, hostname_of
from tests.conftest import build_web

SRC = str(Path(__file__).resolve().parent.parent / "src")
TOKEN = "t" * 43

# ---------------------------------------------------------------------------------------------------- host names


@pytest.mark.parametrize("header,name", [
    ("127.0.0.1:4533", "127.0.0.1"),
    ("LOCALHOST", "localhost"),
    ("[::1]:4533", "[::1]"),
    ("[FD00::1]:80", "[fd00::1]"),
    ("musicpi.local", "musicpi.local"),
    ("  Example.COM:8080  ", "example.com"),
    ("", ""),
])
def test_a_host_header_is_its_lower_case_name_without_the_port(header: str, name: str) -> None:
    assert hostname_of(header) == name


def test_the_loopback_names_are_exactly_these() -> None:
    assert LOOPBACK_HOSTS == {"127.0.0.1", "localhost", "[::1]", "testserver"}


def test_allowed_names_are_lower_case_each_once_in_the_order_given() -> None:
    assert clean_names(["MusicPi.local", " 192.168.1.50 ", "musicpi.local", "pi_1.tail-123.ts.net", "fd00::1", "[fd00::2]"]) == [
        "musicpi.local", "192.168.1.50", "pi_1.tail-123.ts.net", "[fd00::1]", "[fd00::2]",
    ]
    assert clean_names([]) == []


@pytest.mark.parametrize("entry,hint", [
    ("", "empty"),
    ("   ", "empty"),
    ("*.local", "wildcards"),
    ("musicpi.*", "wildcards"),
    ("http://musicpi.local", "name only"),
    ("musicpi.local/music", "name only"),
    ("musicpi.local:4533", "without a port"),
    ("[fd00::1]:4533", "without a port"),
    ("bad name", "not a host name"),
    ("-leading.dash", "not a host name"),
    ("trailing.dot.", "not a host name"),
    ("é.local", "not a host name"),
    ("fd00::g", "not a host name"),
])
def test_an_entry_that_is_not_a_plain_name_or_address_is_refused_and_named(entry: str, hint: str) -> None:
    with pytest.raises(ValueError) as refused:
        clean_names(["fine.local", entry])
    assert hint in str(refused.value)
    if entry.strip():
        assert repr(entry) in str(refused.value)


# ---------------------------------------------------------------------------------------------------- config


def test_the_server_section_has_safe_defaults(tmp_path: Path) -> None:
    section = Config().server
    assert (section.host, section.port, section.allowed_hosts, section.rescan_hours, section.login_days) == ("127.0.0.1", 4533, [], 0, 90)
    assert load_config(tmp_path / "missing.toml").server == ServerConfig()


def test_the_server_section_is_read_saved_and_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[server]\nhost = "0.0.0.0"\nport = 8000\nallowed_hosts = ["musicpi.local", "pi.tail1.ts.net"]\nrescan_hours = 6\nlogin_days = 30\nfuture = true\n', encoding="utf-8")
    read = load_config(path)
    assert read.server == ServerConfig("0.0.0.0", 8000, ["musicpi.local", "pi.tail1.ts.net"], 6, 30)
    save_config(path, read)
    assert load_config(path).server == read.server


def test_a_config_written_before_the_server_section_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[library]\nroots = ["/music"]\n', encoding="utf-8")
    loaded = load_config(path)
    assert loaded.library.roots == ["/music"] and loaded.server == ServerConfig()


def test_the_server_settings_cannot_be_changed_over_http(tmp_path: Path) -> None:
    web = build_web(tmp_path, None)
    assert "server" not in web.client.get("/api/settings").json()
    refused = web.client.put("/api/settings", json={"server": {"allowed_hosts": ["evil.example"]}})
    assert refused.status_code == 422 and "server" in refused.json()["detail"]
    assert web.config.server.allowed_hosts == []


# ---------------------------------------------------------------------------------------------------- what serve decides


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1", "localhost", " LOCALHOST "])
def test_listening_on_this_computer_needs_no_names(bind: str) -> None:
    assert server_ops.is_loopback_bind(bind)
    assert server_ops.allowed_names(bind, []) == []


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.168.1.50", "musicpi.local"])
def test_listening_where_other_devices_can_reach_it_needs_the_names_they_use(bind: str) -> None:
    assert not server_ops.is_loopback_bind(bind)
    with pytest.raises(server_ops.ServeError, match=r"allowed_hosts is empty"):
        server_ops.allowed_names(bind, [])
    assert server_ops.allowed_names(bind, ["MusicPi.local"]) == ["musicpi.local"]


def test_a_bad_name_stops_the_server_and_says_where_it_is() -> None:
    with pytest.raises(server_ops.ServeError, match=r"^\[server\] allowed_hosts: '\*\.local': wildcards are not accepted; list each name\.$"):
        server_ops.allowed_names("127.0.0.1", ["*.local"])


@pytest.mark.parametrize("port,ok", [(1, True), (4533, True), (65535, True), (0, False), (65536, False), (-1, False)])
def test_a_port_is_a_number_from_1_to_65535(port: int, ok: bool) -> None:
    if ok:
        assert server_ops.check_port(port) == port
    else:
        with pytest.raises(server_ops.ServeError, match=rf"^{port} is not a port number"):
            server_ops.check_port(port)


def test_the_token_from_the_environment_wins_over_a_file(tmp_path: Path) -> None:
    (tmp_path / "server.token").write_text("f" * 30, encoding="utf-8")
    assert server_ops.server_token(tmp_path, {"MTK_TOKEN": f"  {TOKEN}  "}) == (TOKEN, "the MTK_TOKEN environment variable")


def test_a_short_token_from_the_environment_is_refused() -> None:
    with pytest.raises(server_ops.ServeError, match=r"^MTK_TOKEN is too short to be a secret: use at least 20 characters"):
        server_ops.server_token(Path("."), {"MTK_TOKEN": "x" * 19})
    assert server_ops.server_token(Path("."), {"MTK_TOKEN": "x" * 20})[0] == "x" * 20


def test_the_first_start_makes_a_token_file_and_every_later_start_reads_the_same_one(tmp_path: Path) -> None:
    data = tmp_path / "state"
    token, where = server_ops.server_token(data, {})
    path = data / "server.token"
    assert where == str(path) and path.read_text(encoding="utf-8") == token + "\n"
    assert len(token) >= 43 and token.replace("-", "").replace("_", "").isalnum()
    assert server_ops.server_token(data, {}) == (token, str(path))
    assert server_ops.server_token(data, {"MTK_TOKEN": ""})[0] == token  # (an empty variable is no variable)
    assert server_ops.server_token(tmp_path / "other", {})[0] != token


@pytest.mark.skipif(os.name == "nt", reason="file permissions")
def test_the_token_file_can_be_read_by_its_owner_only(tmp_path: Path) -> None:
    server_ops.server_token(tmp_path, {})
    assert (tmp_path / "server.token").stat().st_mode & 0o777 == 0o600
    assert server_ops.token_file_warning(tmp_path) is None
    (tmp_path / "server.token").chmod(0o644)
    assert server_ops.token_file_warning(tmp_path) == f"{tmp_path / 'server.token'} can be read by other users of this computer; chmod 600 it."
    assert server_ops.token_file_warning(tmp_path / "nowhere") is None


@pytest.mark.parametrize("content", ["", "\n", "short"])
def test_a_token_file_with_nothing_worth_the_name_is_refused_not_overwritten(tmp_path: Path, content: str) -> None:
    (tmp_path / "server.token").write_text(content, encoding="utf-8")
    with pytest.raises(server_ops.ServeError, match=r"is empty or too short to be a secret"):
        server_ops.server_token(tmp_path, {})
    assert (tmp_path / "server.token").read_text(encoding="utf-8") == content


def test_asking_for_the_token_does_not_make_a_file(tmp_path: Path) -> None:
    with pytest.raises(server_ops.ServeError, match=r"there is no token yet .*does not exist\). Start the server once"):
        server_ops.server_token(tmp_path, {}, create=False)
    assert not (tmp_path / "server.token").exists()
    assert server_ops.server_token(tmp_path, {"MTK_TOKEN": TOKEN}, create=False)[0] == TOKEN


def test_the_sign_in_link_is_the_address_with_the_token() -> None:
    assert server_ops.sign_in_link("https://pi.tail1.ts.net/", TOKEN) == f"https://pi.tail1.ts.net/?token={TOKEN}"
    assert server_ops.sign_in_link("https://x", "ab+cd/ef==gh ij&k") == "https://x/?token=ab%2Bcd%2Fef%3D%3Dgh%20ij%26k"  # (a token picked by hand: its characters are not an address's)
    assert server_ops.sign_in_link(" http://musicpi.local:4533 ", TOKEN) == f"http://musicpi.local:4533/?token={TOKEN}"
    with pytest.raises(server_ops.ServeError, match=r"must start with http:// or https://"):
        server_ops.sign_in_link("musicpi.local", TOKEN)


# ---------------------------------------------------------------------------------------------------- the app's guard


def test_a_server_answers_to_the_names_it_was_given_and_no_others(tmp_path: Path) -> None:
    web = build_web(tmp_path, None, allowed_hosts=["MusicPi.local", "pi.tail1.ts.net"])
    for host in ("musicpi.local", "MUSICPI.LOCAL:4533", "pi.tail1.ts.net", "127.0.0.1:4533", "localhost"):
        assert web.client.get("/api/about", headers={"host": host}).status_code == 200, host
    for host in ("evil.example", "musicpi.local.evil.example", "xmusicpi.local", "tail1.ts.net", "192.168.1.50:4533", ""):
        refused = web.client.get("/api/about", headers={"host": host})
        assert (refused.status_code, refused.json()) == (403, {"detail": "forbidden host"}), host


def test_without_names_only_the_loopback_ones_are_answered_as_before(empty_web) -> None:
    assert empty_web.client.get("/api/about", headers={"host": "musicpi.local"}).status_code == 403
    assert empty_web.client.get("/api/about", headers={"host": "127.0.0.1:4533"}).status_code == 200


def test_a_name_that_is_not_a_plain_name_stops_the_app_from_being_made(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"wildcards are not accepted"):
        build_web(tmp_path, None, allowed_hosts=["*.local"])


def test_the_cookie_a_server_gives_lasts_the_login_days_and_one_for_the_desktop_ends_with_the_window(tmp_path: Path) -> None:
    server = build_web(tmp_path / "a", None, token=TOKEN, login_days=90)
    cookie = server.client.get("/", params={"token": TOKEN}).headers["set-cookie"].lower()
    assert "max-age=7776000" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    desktop = build_web(tmp_path / "b", None, token=TOKEN)
    cookie = desktop.client.get("/", params={"token": TOKEN}).headers["set-cookie"].lower()
    assert "max-age" not in cookie and "expires" not in cookie


def test_the_cookie_is_secure_when_the_page_came_over_https_and_not_otherwise(tmp_path: Path) -> None:
    web = build_web(tmp_path, None, token=TOKEN)
    assert "secure" not in web.client.get("http://testserver/", params={"token": TOKEN}).headers["set-cookie"].lower().replace("samesite", "")
    assert "; secure" in web.client.get("https://testserver/", params={"token": TOKEN}).headers["set-cookie"].lower()


def test_a_token_picked_by_hand_with_characters_that_mean_something_in_an_address_still_signs_a_device_in(tmp_path: Path) -> None:
    token = "q+w/e=r" * 4  # (what `openssl rand -base64` makes looks like this)
    web = build_web(tmp_path, None, token=token, login_days=90)
    opened = web.client.get(server_ops.sign_in_link("http://testserver", token))
    assert opened.status_code == 200 and "set-cookie" in opened.headers
    assert web.client.get("/api/about").status_code == 200  # (the cookie, quoted as cookies are, came back as it went)
    other = build_web(tmp_path / "second", None, token=token)
    assert other.client.get("/", params={"token": token.replace("+", " ")}).status_code == 401  # (written without quoting, the + is a space: not the token)


def test_a_wrong_token_gives_no_cookie_however_long_the_login(tmp_path: Path) -> None:
    web = build_web(tmp_path, None, token=TOKEN, login_days=90)
    refused = web.client.get("/", params={"token": TOKEN[:-1] + "x"})
    assert refused.status_code == 401 and "set-cookie" not in refused.headers


# ---------------------------------------------------------------------------------------------------- the health check


def test_the_health_check_needs_no_token_but_does_need_a_known_name(tmp_path: Path, music_dir: Path) -> None:
    web = build_web(tmp_path, music_dir, token=TOKEN, allowed_hosts=["musicpi.local"])
    answer = web.client.get("/healthz")
    assert answer.status_code == 200
    assert answer.json() == {"status": "ok", "version": __version__, "library": {"folders": 1, "available": 1}}
    assert web.client.get("/healthz", headers={"host": "musicpi.local"}).status_code == 200
    assert web.client.get("/healthz", headers={"host": "evil.example"}).status_code == 403
    assert web.client.get("/api/about").status_code == 401  # (the rest still wants the token)


def test_the_health_check_says_degraded_when_a_library_folder_is_not_there_and_never_names_it(tmp_path: Path, music_dir: Path) -> None:
    web = build_web(tmp_path, music_dir)
    web.config.library.roots.append(str(tmp_path / "unplugged" / "Secret Folder"))
    answer = web.client.get("/healthz")
    assert answer.json() == {"status": "degraded", "version": __version__, "library": {"folders": 2, "available": 1}}
    assert "Secret Folder" not in answer.text and str(music_dir) not in answer.text


def test_the_health_check_answers_a_head_request_and_a_library_with_no_folders_is_ok(empty_web) -> None:
    assert empty_web.client.head("/healthz").status_code == 200
    assert empty_web.client.get("/healthz").json()["status"] == "ok"


# ---------------------------------------------------------------------------------------------------- the commands


def _config(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body + '\n[database]\npath = "./data/library.db"\n[logging]\ndir = "./logs"\n', encoding="utf-8")
    return path


def test_serve_refuses_to_listen_beyond_this_computer_without_names(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path)), "serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "Cannot start the server: it would listen on 0.0.0.0" in result.output and 'allowed_hosts = ["musicpi.local"]' in result.output
    assert not (tmp_path / "server.token").exists()  # (nothing was made on the way to refusing)


def test_serve_reads_the_host_from_the_config_too(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path, '[server]\nhost = "::"')), "serve"])
    assert result.exit_code == 2 and "it would listen on ::" in result.output


@pytest.mark.parametrize("args,words", [
    (["--port", "70000"], "70000 is not a port number"),
    (["--port", "0"], "0 is not a port number"),
])
def test_serve_refuses_a_port_that_is_not_one(tmp_path: Path, args: list[str], words: str) -> None:
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path)), "serve", *args])
    assert result.exit_code == 2 and words in result.output


def test_serve_refuses_a_name_that_is_not_a_plain_name(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path, '[server]\nallowed_hosts = ["*.local"]')), "serve"])
    assert result.exit_code == 2 and "[server] allowed_hosts: '*.local': wildcards are not accepted" in result.output


def test_serve_refuses_a_short_token_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MTK_TOKEN", "short")
    result = CliRunner().invoke(cli_app, ["--config", str(_config(tmp_path)), "serve"])
    assert result.exit_code == 2 and "MTK_TOKEN is too short" in result.output


def test_token_shows_the_token_or_the_link_and_makes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MTK_TOKEN", raising=False)
    config = str(_config(tmp_path))
    missing = CliRunner().invoke(cli_app, ["--config", config, "token"])
    assert missing.exit_code == 1 and "there is no token yet" in missing.output and not (tmp_path / "server.token").exists()
    server_ops.server_token(tmp_path, {})
    saved = (tmp_path / "server.token").read_text(encoding="utf-8").strip()
    assert CliRunner().invoke(cli_app, ["--config", config, "token"]).output.strip() == saved
    linked = CliRunner().invoke(cli_app, ["--config", config, "token", "--url", "https://pi.tail1.ts.net"])
    assert linked.output.strip() == f"https://pi.tail1.ts.net/?token={saved}"
    bad = CliRunner().invoke(cli_app, ["--config", config, "token", "--url", "pi.tail1.ts.net"])
    assert bad.exit_code == 1 and "must start with http" in bad.output
    monkeypatch.setenv("MTK_TOKEN", TOKEN)
    assert CliRunner().invoke(cli_app, ["--config", config, "token"]).output.strip() == TOKEN


# ---------------------------------------------------------------------------------------------------- the real thing


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port: int, path: str, host: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={"Host": host, **(headers or {})})
        answer = conn.getresponse()
        return answer.status, {k.lower(): v for k, v in answer.getheaders()}, answer.read()
    finally:
        conn.close()


def test_a_real_server_answers_the_names_it_was_given_signs_a_device_in_for_long_and_stops_when_asked(tmp_path: Path) -> None:
    port = _free_port()
    config = _config(tmp_path, f'[server]\nport = {port}\nallowed_hosts = ["musicpi.test"]\nlogin_days = 30\n')
    env = {key: value for key, value in os.environ.items() if key != "MTK_TOKEN"}
    env["PYTHONPATH"] = SRC
    process = subprocess.Popen(
        [sys.executable, "-c", "from musictoolkit.cli import app; app()", "--config", str(config), "serve"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path,
    )
    try:
        deadline = time.monotonic() + 40
        while True:
            try:
                status, _, body = _get(port, "/healthz", f"127.0.0.1:{port}")
                break
            except OSError:
                assert process.poll() is None, process.stderr.read()
                assert time.monotonic() < deadline, "the server did not start"
                time.sleep(0.2)
        assert status == 200 and json.loads(body)["status"] == "ok"
        token = (tmp_path / "server.token").read_text(encoding="utf-8").strip()
        assert _get(port, "/healthz", f"musicpi.test:{port}")[0] == 200
        assert _get(port, "/healthz", "evil.example")[0] == 403
        assert _get(port, "/api/about", "musicpi.test")[0] == 401
        assert _get(port, "/api/about", "musicpi.test", {"x-mtk-token": token})[0] == 200
        status, headers, _ = _get(port, f"/?token={token}", "musicpi.test")
        assert status == 200 and "max-age=2592000" in headers["set-cookie"].lower() and "secure" not in headers["set-cookie"].lower().replace("samesite", "")
        status, headers, _ = _get(port, f"/?token={token}", "musicpi.test", {"X-Forwarded-Proto": "https"})
        assert "; secure" in headers["set-cookie"].lower()  # (a proxy on this computer said the page came over https)
    finally:
        if process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)  # (what a service manager sends)
        try:
            out, err = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    assert token not in out and token not in err  # (the token is never printed)
    assert "listening on 127.0.0.1" in out and "musicpi.test" in out
    if os.name != "nt":  # (stopped by SIGTERM the way a service manager stops it, within the time given: uvicorn finishes up, then lets the signal end the process)
        assert process.returncode in (0, -signal.SIGTERM)
