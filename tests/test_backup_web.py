"""Copies of the database where a person meets them: the button and what it says, the health check, the app making a copy by itself, the settings in config.toml
(read, checked, refused by the server when wrong) and the `mtk backup` commands."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from musictoolkit import backup
from musictoolkit.cli import app as cli_app
from musictoolkit.config import Config, load_config, save_config
from musictoolkit.db.connection import connect
from musictoolkit.web import backup_job
from musictoolkit.web.backup_job import BackupSchedule
from tests.conftest import build_web, ids_by_title
from tests.test_backup import aged, counts, fill, touch
from tests.test_backup_schedule import HOUR, Clock

TOKEN = "t" * 43
GONE = "the folder is not there (is the drive connected?)"


@pytest.fixture
def drive(tmp_path: Path) -> Path:
    folder = tmp_path / "drive" / "backups"
    folder.mkdir(parents=True)
    return folder


def made_in(folder: Path) -> list[str]:
    return [copy.path.name for copy in backup.listing(folder)]


# ------------------------------------------------------------------------------------------------------------------------------ the button


def test_the_button_makes_a_whole_copy_in_the_apps_own_folder_and_says_where_and_how_big(web) -> None:
    web.client.post("/api/plays", json={"track_id": ids_by_title(web.client)["Wires"], "ms_played": 1000})
    answer = web.client.post("/api/system/backup")
    assert answer.status_code == 200
    body = answer.json()
    made = Path(body["path"])
    assert made.parent == web.ctx.data_dir / "backups" and backup.NAME.match(made.name) and body["size"] == made.stat().st_size > 0
    assert body["copies"] == [str(made)] and body["problems"] == []
    with web.ctx.db() as conn:
        live = {name: conn.execute(query).fetchone()[0] for name, query in backup.TABLES.items()}
    assert live["songs"] == 9 and live["plays"] == 1 and counts(made) == live
    assert backup.check(made).schema == backup.latest_schema()


def test_the_button_puts_the_copy_in_every_folder_the_config_names(web, drive: Path) -> None:
    web.config.database.backup_folders = [str(drive)]
    body = web.client.post("/api/system/backup").json()
    assert len(body["copies"]) == 2 and body["copies"][1] == str(drive / Path(body["copies"][0]).name) and body["problems"] == []
    assert made_in(drive) == made_in(web.ctx.data_dir / "backups") == [Path(body["path"]).name]
    assert (drive / Path(body["path"]).name).read_bytes() == Path(body["path"]).read_bytes()


def test_a_folder_that_is_not_there_is_said_and_the_copy_that_could_be_made_is_still_made(web, tmp_path: Path) -> None:
    web.config.database.backup_folders = [str(tmp_path / "unplugged" / "backups")]
    answer = web.client.post("/api/system/backup")
    body = answer.json()
    assert answer.status_code == 200 and Path(body["path"]).is_file() and body["copies"] == [body["path"]]
    assert body["problems"] == [{"folder": str(tmp_path / "unplugged" / "backups"), "problem": GONE}]
    assert not (tmp_path / "unplugged").exists()


def test_when_no_copy_could_be_made_the_button_says_why_and_it_is_an_error(web, tmp_path: Path) -> None:
    web.ctx.db_path = tmp_path / "no" / "such" / "library.db"
    answer = web.client.post("/api/system/backup")
    assert answer.status_code == 500 and answer.json()["detail"] == "No copy was made: there is no database to copy"
    assert made_in(web.ctx.data_dir / "backups") == []


def test_the_copy_is_made_of_the_database_the_app_has_open_and_not_of_a_stale_file(web) -> None:
    track = ids_by_title(web.client)["Wires"]
    for _ in range(3):
        web.client.post("/api/plays", json={"track_id": track, "ms_played": 1000})
    assert web.client.patch(f"/api/tracks/{track}", json={"favorite": True, "rating": 4}).status_code == 200
    made = Path(web.client.post("/api/system/backup").json()["path"])
    assert counts(made)["plays"] == 3 and counts(made)["favorites"] == 1
    assert not list((web.ctx.data_dir / "backups").glob("*.partial")) and not list((web.ctx.data_dir / "backups").glob("*-wal"))


def test_a_listing_of_the_copies_says_the_settings_what_each_folder_holds_and_the_last_run(web, drive: Path) -> None:
    web.config.database.backup_folders = [str(drive)]
    before = web.client.get("/api/system/backups").json()
    assert set(before) == {"hours", "keep", "folders", "last", "overdue"}
    assert (before["hours"], before["keep"], before["last"], before["overdue"]) == (24, 7, None, False)
    assert [(f["folder"], f["there"], f["copies"]) for f in before["folders"]] == [(str(web.ctx.data_dir / "backups"), False, 0), (str(drive), True, 0)]
    web.client.post("/api/system/backup")
    after = web.client.get("/api/system/backups").json()
    assert [(f["copies"], f["there"]) for f in after["folders"]] == [(1, True), (1, True)]
    assert all(f["size"] > 0 and f["newest"] for f in after["folders"]) and after["last"]["problems"] == [] and after["last"]["when"]


def test_a_setting_that_is_wrong_does_not_stop_the_button_or_the_listing(web) -> None:
    web.config.database.backup_keep = 0
    web.config.database.backup_folders = "/not/a/list"  # type: ignore[assignment]
    assert web.client.post("/api/system/backup").status_code == 200
    listing = web.client.get("/api/system/backups").json()
    assert (listing["hours"], listing["keep"]) == (24, 7) and len(listing["folders"]) == 1


def test_the_button_and_the_listing_need_the_token_like_every_other_route(tmp_path: Path) -> None:
    web = build_web(tmp_path, None, token=TOKEN)
    for method, path in (("post", "/api/system/backup"), ("get", "/api/system/backups")):
        assert getattr(web.client, method)(path).status_code == 401
        assert getattr(web.client, method)(path, headers={"x-mtk-token": "x" * 43}).status_code == 401
        assert getattr(web.client, method)(path, headers={"x-mtk-token": TOKEN}).status_code == 200
    assert len(made_in(tmp_path / "backups")) == 1  # (the one asked for with the token; the two that were refused made none)


def test_copies_asked_for_by_the_button_are_kept_to_the_newest_each_folder_keeps(web) -> None:
    web.config.database.backup_keep = 2
    clock = Clock()
    web.ctx.backups = BackupSchedule(web.ctx, clock=clock)
    for _ in range(4):
        clock.pass_hours(25)  # (the name has the second in it: a day apart, each is a copy of its own)
        assert web.client.post("/api/system/backup").status_code == 200
    assert len(made_in(web.ctx.data_dir / "backups")) == 2


# ----------------------------------------------------------------------------------------------------------------------------- the health check


def test_the_health_check_says_whether_copies_are_being_made(tmp_path: Path) -> None:
    web = build_web(tmp_path, None)
    assert web.client.get("/healthz").json()["backups"] == "ok"
    web.config.database.backup_hours = 0
    answer = web.client.get("/healthz").json()
    assert answer["backups"] == "off" and answer["status"] == "ok"  # (copies that were turned off are not a fault)
    for hours in (0.5, 1):
        web.config.database.backup_hours = hours
        assert web.client.get("/healthz").json()["backups"] == "ok"  # (an hour, or less than an hour, is still copies being made)


def test_the_health_check_is_degraded_when_the_copies_are_late_and_ok_again_after_one_is_made(tmp_path: Path) -> None:
    web = build_web(tmp_path, None)
    clock = Clock()
    web.ctx.backups = BackupSchedule(web.ctx, clock=clock)
    clock.ahead = backup_job.GRACE + 1  # (the app has been up ten minutes and has made no copy)
    late = web.client.get("/healthz").json()
    assert (late["status"], late["backups"], late["library"]) == ("degraded", "overdue", {"folders": 0, "available": 0})
    assert web.client.head("/healthz").status_code == 200  # (a monitor reads the answer, not the status code)
    assert web.client.post("/api/system/backup").status_code == 200
    assert web.client.get("/healthz").json()["status"] == "ok" and web.client.get("/healthz").json()["backups"] == "ok"
    clock.pass_hours(73)
    assert web.client.get("/healthz").json()["backups"] == "overdue"


def test_the_health_check_is_degraded_when_a_folder_the_config_names_gets_no_copies(tmp_path: Path) -> None:
    web = build_web(tmp_path, None)
    web.config.database.backup_folders = [str(tmp_path / "unmounted" / "backups")]
    clock = Clock()
    web.ctx.backups = BackupSchedule(web.ctx, clock=clock)
    assert web.client.post("/api/system/backup").status_code == 200  # (the app's own folder has a new copy, the named one cannot have one)
    assert web.client.get("/healthz").json()["backups"] == "ok"  # (the app has only just started)
    clock.ahead = backup_job.GRACE + 1
    late = web.client.get("/healthz").json()
    assert (late["status"], late["backups"]) == ("degraded", "overdue") and "unmounted" not in str(late)


def test_the_health_check_names_no_folder_and_needs_no_token(tmp_path: Path, drive: Path) -> None:
    web = build_web(tmp_path, None, token=TOKEN)
    web.config.database.backup_folders = [str(drive)]
    answer = TestClient(web.app).get("/healthz")
    assert answer.status_code == 200 and answer.json()["backups"] == "ok" and str(drive) not in answer.text and "backups/" not in answer.text


# ------------------------------------------------------------------------------------------------------------- the app making a copy by itself


def test_an_app_that_is_up_makes_its_first_copy_by_itself_and_not_a_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_job, "CHECK_EVERY", 0.01)
    web = build_web(tmp_path, None)
    folder = tmp_path / "backups"
    with TestClient(web.app):
        thread = web.ctx.backups._thread
        assert thread is not None and thread.is_alive() and thread.name == "database-backup"
        deadline = time.monotonic() + 10
        while not made_in(folder) and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # (the thread has looked many times more: it is not time again)
        assert len(made_in(folder)) == 1 and backup.check(folder / made_in(folder)[0]).ok
    assert not thread.is_alive() and web.ctx.backups._thread is None


def test_with_copies_by_themselves_turned_off_the_app_makes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_job, "CHECK_EVERY", 0.01)
    web = build_web(tmp_path, None)
    web.config.database.backup_hours = 0
    with TestClient(web.app):
        time.sleep(0.3)
    assert not (tmp_path / "backups").exists()


def test_an_app_that_was_stopped_and_started_again_does_not_start_the_count_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_job, "CHECK_EVERY", 0.01)
    first = build_web(tmp_path, None)
    first.ctx.backups.run_now()
    again = build_web(tmp_path, None)  # (the same data folder: the copy that is there is a few seconds old)
    with TestClient(again.app):
        time.sleep(0.3)
    assert len(made_in(tmp_path / "backups")) == 1


# ------------------------------------------------------------------------------------------------------------------------------ the settings


def write(path: Path, database: str = "") -> Path:
    path.write_text(f'[database]\npath = "./data/library.db"\n{database}\n[logging]\ndir = "./logs"\n', encoding="utf-8")
    return path


def test_the_settings_are_read_from_the_database_section_and_a_folder_is_relative_to_the_file(tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "backups"
    folder = tmp_path / "conf"
    folder.mkdir()
    database = load_config(write(folder / "config.toml", f'backup_hours = 6\nbackup_keep = 3\nbackup_folders = ["{absolute.as_posix()}", "../drive/backups", "~/mtk-backups"]')).database
    assert (database.backup_hours, database.backup_keep) == (6, 3)
    assert database.backup_folders == [str(absolute), str(folder / ".." / "drive" / "backups"), str(Path("~/mtk-backups").expanduser())]
    assert backup.settings_of(database) == backup.Settings(6, 3, tuple(Path(folder) for folder in database.backup_folders))


def test_with_nothing_said_a_copy_is_made_every_day_and_seven_are_kept(tmp_path: Path) -> None:
    database = load_config(write(tmp_path / "config.toml")).database
    assert (database.backup_hours, database.backup_keep, database.backup_folders) == (24, 7, [])
    assert load_config(tmp_path / "nothing-here.toml").database.backup_hours == 24
    assert (Config().database.backup_hours, Config().database.backup_keep, Config().database.backup_folders) == (24, 7, [])


@pytest.mark.parametrize("line", ['backup_hours = "daily"', "backup_keep = 0", 'backup_folders = "/srv/music/backups"', "backup_folders = [3]", 'backup_folders = [""]'])
def test_a_setting_that_is_wrong_is_still_read_and_said_when_the_copies_are_asked_for(tmp_path: Path, line: str) -> None:
    database = load_config(write(tmp_path / "config.toml", line)).database  # (loading must not fail: the sign-in page and the rest of the app need the config)
    with pytest.raises(backup.BackupError):
        backup.settings_of(database)


def test_the_settings_survive_being_saved_and_read_again(tmp_path: Path) -> None:
    config = Config()
    config.database.backup_hours = 12.5
    config.database.backup_keep = 4
    config.database.backup_folders = [str(tmp_path / "drive")]
    save_config(tmp_path / "config.toml", config)
    again = load_config(tmp_path / "config.toml").database
    assert (again.backup_hours, again.backup_keep, again.backup_folders) == (12.5, 4, [str(tmp_path / "drive")])


def config_for(tmp_path: Path, database: str = "") -> Path:
    return write(tmp_path / "config.toml", database)


@pytest.fixture
def not_listening(monkeypatch: pytest.MonkeyPatch) -> dict:
    """`mtk serve` without really listening: whether it got as far as asking uvicorn to run."""
    import uvicorn

    seen: dict = {"run": None}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: seen.update(run=options))
    monkeypatch.delenv("MTK_TOKEN", raising=False)
    return seen


@pytest.mark.parametrize("line,said", [
    ('backup_hours = "daily"', "[database] backup_hours must be a number of hours, 0 or more"),
    ("backup_hours = -2", "[database] backup_hours must be a number of hours, 0 or more"),
    ("backup_keep = 0", "[database] backup_keep must be a whole number, 1 or more."),
    ("backup_keep = 2.5", "[database] backup_keep must be a whole number, 1 or more."),
    ('backup_folders = "/srv/music/backups"', '[database] backup_folders must be a list of folders in quotes, for example backup_folders = ["/srv/music/backups"].'),
])
def test_serve_refuses_to_start_with_a_setting_for_copies_that_is_wrong_and_says_which(tmp_path: Path, not_listening: dict, line: str, said: str) -> None:
    result = CliRunner().invoke(cli_app, ["--config", str(config_for(tmp_path, line)), "serve", "--host", "127.0.0.1"])
    assert result.exit_code == 2 and f"Cannot start the server: {said}" in result.output
    assert not_listening["run"] is None and not (tmp_path / "server.token").exists()


def test_serve_starts_with_settings_for_copies_that_are_right_and_with_none(tmp_path: Path, not_listening: dict, drive: Path) -> None:
    for database in ("", f'backup_hours = 0\nbackup_keep = 3\nbackup_folders = ["{drive.as_posix()}"]'):
        result = CliRunner().invoke(cli_app, ["--config", str(config_for(tmp_path, database)), "serve", "--host", "127.0.0.1"])
        assert result.exit_code == 0, result.output
        assert not_listening["run"] is not None
        not_listening["run"] = None


# ------------------------------------------------------------------------------------------------------------------------- `mtk backup`


@pytest.fixture
def installed(tmp_path: Path, drive: Path) -> dict:
    """A config, a library database with a few songs in it, and a drive that is there."""
    config = config_for(tmp_path, f'backup_keep = 2\nbackup_folders = ["{drive.as_posix()}"]')
    fill(tmp_path / "data" / "library.db")
    return {"config": str(config), "db": tmp_path / "data" / "library.db", "primary": tmp_path / "backups", "drive": drive}


def run(installed: dict, *args: str):
    return CliRunner().invoke(cli_app, ["--config", installed["config"], "backup", *args])


def sized(number: int) -> str:
    return f"{number / 1e6:,.1f} MB"


def lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def stopped(result, code: int = 1) -> None:
    """The command ended by saying it could not (an exit with that code), and not by falling over."""
    assert result.exit_code == code and isinstance(result.exception, SystemExit), (result.exit_code, repr(result.exception))


def forget_the_plays(db: Path) -> None:
    """What a person would be sorry to lose: the history of what was played."""
    conn = connect(db)
    conn.execute("DELETE FROM play_history")
    conn.commit()
    conn.close()


def test_backup_now_makes_a_copy_in_each_folder_and_says_where_how_big_and_what_was_taken_away(installed: dict) -> None:
    result = run(installed, "now")
    assert result.exit_code == 0 and result.exception is None and result.stderr == "", result.output
    [made] = backup.listing(installed["primary"])
    size = f"{made.size / 1e6:,.1f} MB"
    assert lines(result.stdout) == [f"  {made.path}  ({size}; 0 older taken away)", f"  {installed['drive'] / made.path.name}  ({size}; 0 older taken away)"]
    assert made_in(installed["drive"]) == [made.path.name] and counts(made.path)["songs"] == 5


def test_backup_now_keeps_the_newest_few_in_each_folder_and_says_how_many_were_taken_away(installed: dict) -> None:
    for number in range(3):
        for folder in (installed["primary"], installed["drive"]):
            touch(folder, f"library-2026010{number + 1}-120000.db", (30 - number) * 24 * HOUR)
    result = run(installed, "now")
    assert result.exit_code == 0 and result.stdout.count("; 2 older taken away)") == 2, result.output
    assert len(made_in(installed["primary"])) == len(made_in(installed["drive"])) == 2


def test_backup_now_says_which_folder_failed_on_the_error_stream_and_ends_with_an_error_after_doing_the_rest(installed: dict) -> None:
    installed["drive"].rmdir()
    result = run(installed, "now")
    stopped(result)
    assert lines(result.stderr) == [f"  {installed['drive']}: {GONE}"] and f"{GONE}" not in result.stdout
    [made] = backup.listing(installed["primary"])  # (the app's own folder got its copy, and it is said)
    assert lines(result.stdout) == [f"  {made.path}  ({made.size / 1e6:,.1f} MB; 0 older taken away)"]


def test_backup_now_with_no_database_says_so_and_makes_nothing(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["--config", str(config_for(tmp_path)), "backup", "now"])
    stopped(result)
    assert lines(result.stderr) == [f"  {tmp_path / 'backups'}: there is no database to copy"] and result.stdout == "" and not (tmp_path / "data" / "library.db").exists()


def test_backup_with_a_setting_that_is_wrong_says_which_on_the_error_stream_and_does_nothing(tmp_path: Path) -> None:
    fill(tmp_path / "data" / "library.db")
    config = config_for(tmp_path, "backup_keep = 0")
    for command in (["now"], ["list"]):
        result = CliRunner().invoke(cli_app, ["--config", str(config), "backup", *command])
        stopped(result, 2)
        assert lines(result.stderr) == ["[database] backup_keep must be a whole number, 1 or more."] and result.stdout == ""
    assert not (tmp_path / "backups").exists()


def test_backup_list_names_the_copies_of_each_folder_newest_first_and_a_folder_that_is_not_there(installed: dict, tmp_path: Path) -> None:
    run(installed, "now")
    [made] = backup.listing(installed["primary"])
    aged(backup.listing(installed["drive"])[0].path, 5 * 24 * HOUR)
    ahead = installed["primary"] / "library-20990101-000000.db"
    ahead.write_bytes(b"x")
    os.utime(ahead, (time.time() + 10**8, time.time() + 10**8))
    result = run(installed, "list")
    assert result.exit_code == 0 and result.stderr == ""
    old = backup.listing(installed["drive"])[0]
    assert result.stdout.splitlines() == [
        str(installed["primary"]),
        f"  {made.path.name}  {sized(made.size):>10}  {datetime.fromtimestamp(made.made):%Y-%m-%d %H:%M}",
        f"  {ahead.name}  {sized(1):>10}  (dated in the future)",
        str(installed["drive"]),
        f"  {old.path.name}  {sized(old.size):>10}  {datetime.fromtimestamp(old.made):%Y-%m-%d %H:%M}",
    ]
    installed["drive"].rename(tmp_path / "moved")
    assert run(installed, "list").stdout.splitlines()[3] == f"{installed['drive']}   (the folder is not there)"


def test_backup_check_says_sound_and_what_the_copy_holds(installed: dict) -> None:
    run(installed, "now")
    [made] = backup.listing(installed["primary"])
    sound = run(installed, "check", str(made.path))
    assert sound.exit_code == 0 and sound.stderr == ""
    assert lines(sound.stdout) == [f"{made.path}: sound. 5 songs, 3 plays, 1 playlist, 2 favorites. Layout {backup.latest_schema()} (this app's is {backup.latest_schema()})."]


def test_backup_check_says_what_is_wrong_with_a_copy_on_the_error_stream(installed: dict, tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database at all, just some text that is long enough")
    bad = run(installed, "check", str(broken))
    stopped(bad)
    assert lines(bad.stderr) == [f"{broken}: this is not a Music Toolkit database (file is not a database)"] and bad.stdout == ""
    missing = run(installed, "check", str(tmp_path / "nothing.db"))
    stopped(missing)
    assert lines(missing.stderr) == [f"{tmp_path / 'nothing.db'}: unable to open database file"] and missing.stdout == ""


def test_backup_restore_says_what_it_would_do_and_does_not_do_it_without_yes(installed: dict) -> None:
    run(installed, "now")
    [made] = backup.listing(installed["primary"])
    forget_the_plays(installed["db"])
    result = run(installed, "restore", str(made.path))
    stopped(result)
    assert lines(result.stdout) == [f"{made.path} holds 5 songs, 3 plays, 1 playlist, 2 favorites. It would replace {installed['db']}.",
                                    "Nothing was changed. Add --yes to do it (what is in the database now is kept first)."] and result.stderr == ""
    assert counts(installed["db"])["plays"] == 0 and not list(installed["primary"].glob("library-before-restore-*"))


def test_backup_restore_with_yes_puts_the_copy_back_and_keeps_what_was_there(installed: dict) -> None:
    run(installed, "now")
    [made] = backup.listing(installed["primary"])
    forget_the_plays(installed["db"])
    result = run(installed, "restore", str(made.path), "--yes")
    assert result.exit_code == 0 and result.exception is None and result.stderr == "", result.output
    [kept] = installed["primary"].glob("library-before-restore-*.db")
    assert lines(result.stdout)[-1] == f"Done. What was there is kept in {kept}. Start the app again (the server: sudo systemctl restart musictoolkit)."
    assert counts(installed["db"]) == counts(made.path) and counts(installed["db"])["plays"] == 3 and counts(kept)["plays"] == 0


def test_backup_restore_where_there_is_no_database_makes_it_and_has_nothing_to_keep(installed: dict) -> None:
    run(installed, "now")
    [made] = backup.listing(installed["primary"])
    installed["db"].unlink()
    result = run(installed, "restore", str(made.path), "--yes")
    assert result.exit_code == 0 and lines(result.stdout)[-1] == "Done. Start the app again (the server: sudo systemctl restart musictoolkit)."
    assert counts(installed["db"]) == counts(made.path) and not list(installed["primary"].glob("library-before-restore-*"))


def test_backup_restore_of_a_copy_that_is_not_sound_says_so_on_the_error_stream_and_changes_nothing(installed: dict, tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database, but long enough to be read as one by sqlite " * 4)
    before = installed["db"].read_bytes()
    for extra in ((), ("--yes",)):
        result = run(installed, "restore", str(broken), *extra)
        stopped(result)
        assert lines(result.stderr) == [f"{broken} cannot be restored: this is not a Music Toolkit database (file is not a database)"]
        assert result.stdout == ""  # (it did not go on to say what it would do)
    assert installed["db"].read_bytes() == before and not list(installed["primary"].glob("library-before-restore-*"))


def test_backup_restore_of_a_copy_from_a_newer_app_says_to_update_first_and_ends_there(installed: dict, tmp_path: Path) -> None:
    run(installed, "now")
    [made] = backup.listing(installed["primary"])
    newer = tmp_path / "newer.db"
    newer.write_bytes(made.path.read_bytes())
    conn = sqlite3.connect(newer)
    conn.execute(f"PRAGMA user_version = {backup.latest_schema() + 1}")
    conn.commit()
    conn.close()
    result = run(installed, "restore", str(newer), "--yes")
    stopped(result)
    assert lines(result.stderr) == [f"newer.db was made by a newer version of the app (its layout is {backup.latest_schema() + 1}, this one knows {backup.latest_schema()}): update the app first"]
    assert "Done." not in result.stdout and counts(installed["db"])["songs"] == 5


def test_backup_with_no_command_shows_what_there_is_to_do(installed: dict) -> None:
    result = run(installed)
    assert "Usage: mtk backup" in result.output and "Missing command" not in result.output
    for command in ("now", "list", "check", "restore"):
        assert f"│ {command} " in result.output


def test_the_help_of_the_commands_says_what_the_file_is_and_what_yes_does(installed: dict) -> None:
    for command in ("check", "restore"):
        assert re.search(r"A copy of the database \[required\]", run(installed, command, "--help").output)
    assert re.search(r"--yes\s+<bool>\s+Do it: without this it only says what it would do\s+│", run(installed, "restore", "--help").output)
