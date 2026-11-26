"""Copying the database by itself: when it is time (by the date of the newest copy, so that a restart does not start the count again), what is not tried every minute,
what is said when a setting is wrong, and when the health check calls the copies late."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from musictoolkit import backup
from musictoolkit.backup import Outcome
from musictoolkit.config import Config
from musictoolkit.web import backup_job
from musictoolkit.web.backup_job import BackupSchedule
from tests.test_backup import aged, fill, touch

HOUR = 3600.0


class Clock:
    """The clock of the computer, set forward by what the test says (the dates of the files are the computer's own, so the two have to agree)."""

    def __init__(self) -> None:
        self.ahead = 0.0

    def __call__(self) -> float:
        return time.time() + self.ahead

    def pass_hours(self, hours: float) -> None:
        self.ahead += hours * HOUR


@pytest.fixture
def world(tmp_path: Path) -> SimpleNamespace:
    config = Config()
    config.database.path = str(tmp_path / "data" / "library.db")
    config.database.backup_hours = 24
    config.database.backup_keep = 3
    config.database.backup_folders = []
    drive = tmp_path / "drive"
    drive.mkdir()
    clock = Clock()
    ctx = SimpleNamespace(data_dir=tmp_path, db_path=fill(tmp_path / "data" / "library.db"), config=config)
    return SimpleNamespace(ctx=ctx, clock=clock, schedule=BackupSchedule(ctx, clock=clock), drive=drive, tmp=tmp_path, backups=tmp_path / "backups")


def names(folder: Path) -> list[str]:
    return sorted(copy.path.name for copy in backup.listing(folder))


# -------------------------------------------------------------------------------------------------------------------------------- when


def test_with_hours_of_zero_nothing_is_ever_made_by_itself(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_hours = 0
    world.clock.pass_hours(10**6)
    assert world.schedule.tick() is None and not world.backups.exists()


def test_a_copy_is_made_when_there_is_none_and_then_not_again_until_the_hours_have_passed(world: SimpleNamespace) -> None:
    outcomes = world.schedule.tick()
    assert [o.problem for o in outcomes] == [None] and len(backup.listing(world.backups)) == 1
    assert world.schedule.tick() is None
    world.clock.pass_hours(23.9)
    assert world.schedule.tick() is None
    world.clock.pass_hours(0.2)
    assert world.schedule.tick() is not None
    assert len(names(world.backups)) == 2


def test_the_age_of_a_copy_is_the_date_of_its_file_so_a_restart_does_not_start_the_count_again(world: SimpleNamespace) -> None:
    world.schedule.tick()
    restarted = BackupSchedule(world.ctx, clock=world.clock)
    assert restarted.tick() is None  # (the copy is a few seconds old)
    for copy in backup.listing(world.backups):
        aged(copy.path, 25 * HOUR)
    assert restarted.tick() is not None


def test_only_the_newest_copies_are_kept_each_time(world: SimpleNamespace) -> None:
    for _ in range(6):
        world.schedule.tick()
        world.clock.pass_hours(25)
    assert len(backup.listing(world.backups)) == 3


def test_a_copy_is_made_in_every_folder_named(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_folders = [str(world.drive)]
    outcomes = world.schedule.tick()
    assert [o.problem for o in outcomes] == [None, None]
    [here], [there] = backup.listing(world.backups), backup.listing(world.drive)
    assert here.path.name == there.path.name and here.path.read_bytes() == there.path.read_bytes()


def test_a_folder_that_lacks_the_newest_copy_is_given_it_and_no_new_copy_is_made_of_the_database(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_folders = [str(world.drive)]
    world.schedule.tick()
    [only] = backup.listing(world.backups)
    only_there = backup.listing(world.drive)[0].path
    only_there.unlink()  # (the drive was away when the copy was made, and is back)
    assert world.schedule.tick() is None  # (not within the hour after a try)
    world.clock.pass_hours(1.1)
    caught_up = world.schedule.tick()
    assert [(o.folder, o.problem) for o in caught_up] == [(world.drive, None)]  # (the drive only: the app's own folder had it)
    assert [copy.path.name for copy in backup.listing(world.backups)] == [only.path.name] == [copy.path.name for copy in backup.listing(world.drive)]
    assert (world.drive / only.path.name).read_bytes() == only.path.read_bytes()
    world.clock.pass_hours(2)
    assert world.schedule.tick() is None  # (nothing is behind now)


def test_a_folder_that_is_named_after_the_copies_were_made_gets_the_newest_one_when_the_app_is_started_again(world: SimpleNamespace) -> None:
    world.schedule.tick()
    world.ctx.config.database.backup_folders = [str(world.drive)]
    started_again = BackupSchedule(world.ctx, clock=world.clock)
    caught_up = started_again.tick()
    assert [(o.folder, o.problem) for o in caught_up] == [(world.drive, None)] and len(backup.listing(world.backups)) == 1
    assert [copy.path.name for copy in backup.listing(world.drive)] == names(world.backups)


def test_the_app_own_folder_is_made_again_when_it_is_the_one_that_lacks_the_newest_copy(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_folders = [str(world.drive)]
    world.schedule.tick()
    shutil.rmtree(world.backups)  # (someone cleared it out)
    world.clock.pass_hours(1.1)
    caught_up = world.schedule.tick()
    assert [(o.folder, o.problem) for o in caught_up] == [(world.backups, None)] and names(world.backups) == names(world.drive) and len(names(world.drive)) == 1


def test_a_folder_that_cannot_be_written_does_not_make_the_copies_of_the_other_folders_come_every_hour(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    world.ctx.config.database.backup_folders = [str(world.drive)]
    real, tried = backup.copy_to, []

    def refused(copy: Path, folder: Path) -> Path:
        if folder == world.drive:
            tried.append(copy.name)
            raise PermissionError(13, "Permission denied")
        return real(copy, folder)

    monkeypatch.setattr(backup, "copy_to", refused)
    first = world.schedule.tick()
    assert [o.problem for o in first] == [None, "Permission denied"] and len(tried) == 1
    for _ in range(10):  # (ten hours: the drive is tried again each hour; the app's own folder keeps what it had, and is not given a new copy each time)
        world.clock.pass_hours(1.1)
        again = world.schedule.tick()
        assert [(o.folder, o.problem) for o in again] == [(world.drive, "Permission denied")]
    assert len(tried) == 11 and len(names(world.backups)) == 1 and names(world.drive) == []
    world.clock.pass_hours(24)
    assert [o.problem for o in world.schedule.tick()] == [None, "Permission denied"]  # (a new copy when the old one is a day old)
    assert len(names(world.backups)) == 2


def test_the_copy_a_folder_is_given_is_the_newest_of_all_the_folders_and_not_only_of_the_apps_own(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_folders = [str(world.drive)]
    world.schedule.tick()
    for copy in backup.listing(world.backups):
        copy.path.unlink()
    for copy in backup.listing(world.drive):
        aged(copy.path, 3 * HOUR)  # (only the drive has a copy, and it is three hours old)
    world.clock.pass_hours(1.1)
    caught_up = world.schedule.tick()
    assert [(o.folder, o.problem) for o in caught_up] == [(world.backups, None)] and len(names(world.drive)) == 1  # (it comes from the drive: no new copy of the database)
    assert names(world.backups) == names(world.drive)


def test_a_folder_that_is_not_there_does_not_make_it_time_and_is_said_when_a_copy_is_made(world: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    gone = world.tmp / "unmounted" / "backups"
    world.ctx.config.database.backup_folders = [str(gone)]
    with caplog.at_level(logging.INFO, logger="musictoolkit"):
        first = world.schedule.tick()
    assert [o.problem for o in first] == [None, "the folder is not there (is the drive connected?)"] and not gone.exists() and not gone.parent.exists()
    assert any(record.levelno == logging.WARNING and "the folder is not there" in record.getMessage() for record in caplog.records)
    world.clock.pass_hours(2)  # (past the wait after trouble)
    assert world.schedule.tick() is None  # (a folder that is not there does not make a copy due; the app's own copy is two hours old)
    world.clock.pass_hours(23)
    assert world.schedule.tick() is not None


def test_a_run_that_had_trouble_is_tried_again_after_an_hour_and_not_every_minute(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[int] = []

    def failing(*args, **kwargs):
        runs.append(1)
        return [Outcome(world.backups, problem="No space left on device")]

    monkeypatch.setattr(backup, "run", failing)
    assert world.schedule.tick() is not None and len(runs) == 1
    world.clock.pass_hours(0.5)
    assert world.schedule.tick() is None and len(runs) == 1
    world.clock.pass_hours(0.6)
    assert world.schedule.tick() is not None and len(runs) == 2


def test_with_less_than_an_hour_between_copies_a_failed_run_is_tried_again_after_that_time(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    world.ctx.config.database.backup_hours = 0.25
    runs: list[int] = []
    monkeypatch.setattr(backup, "run", lambda *args, **kwargs: runs.append(1) or [Outcome(world.backups, problem="no")])
    world.schedule.tick()
    world.clock.pass_hours(0.2)
    assert world.schedule.tick() is None
    world.clock.pass_hours(0.1)
    assert world.schedule.tick() is not None and len(runs) == 2


# ----------------------------------------------------------------------------------------------------------------------- the button


def test_the_button_makes_a_copy_even_when_copies_by_themselves_are_off_and_when_one_is_fresh(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_hours = 0
    world.ctx.config.database.backup_folders = [str(world.drive)]
    outcomes = world.schedule.run_now()
    assert [o.problem for o in outcomes] == [None, None] and len(backup.listing(world.backups)) == 1 and len(backup.listing(world.drive)) == 1
    assert world.schedule.status()["last"]["problems"] == []


def test_a_copy_is_logged_with_where_and_how_big_and_a_problem_as_a_warning(world: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    world.ctx.config.database.backup_folders = [str(world.tmp / "absent")]
    with caplog.at_level(logging.INFO, logger="musictoolkit"):
        world.schedule.run_now()
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    [made] = backup.listing(world.backups)
    assert infos == [f"Copy of the database: {made.path} ({made.size / 1e6:.1f} MB), 0 older taken away"]
    assert warnings == [f"Copy of the database: {world.tmp / 'absent'}: the folder is not there (is the drive connected?)"]


def test_two_copies_asked_for_at_once_are_made_one_after_the_other(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    inside, overlapped, release = [0], [False], threading.Event()
    real = backup.run

    def slow(*args, **kwargs):
        inside[0] += 1
        overlapped[0] = overlapped[0] or inside[0] > 1
        release.wait(5)
        try:
            return real(*args, **kwargs)
        finally:
            inside[0] -= 1

    monkeypatch.setattr(backup, "run", slow)
    threads = [threading.Thread(target=world.schedule.run_now) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    release.set()
    for thread in threads:
        thread.join(10)
    assert not overlapped[0] and inside[0] == 0


# -------------------------------------------------------------------------------------------------------------------------------- settings


@pytest.mark.parametrize("field,value,said", [
    ("backup_hours", "daily", "[database] backup_hours must be a number of hours, 0 or more (0: no copies by themselves)."),
    ("backup_hours", -1, "[database] backup_hours must be a number of hours, 0 or more (0: no copies by themselves)."),
    ("backup_hours", True, "[database] backup_hours must be a number of hours, 0 or more (0: no copies by themselves)."),
    ("backup_hours", float("inf"), "[database] backup_hours must be a number of hours, 0 or more (0: no copies by themselves)."),
    ("backup_hours", float("nan"), "[database] backup_hours must be a number of hours, 0 or more (0: no copies by themselves)."),
    ("backup_keep", 0, "[database] backup_keep must be a whole number, 1 or more."),
    ("backup_keep", 2.5, "[database] backup_keep must be a whole number, 1 or more."),
    ("backup_keep", "7", "[database] backup_keep must be a whole number, 1 or more."),
    ("backup_keep", False, "[database] backup_keep must be a whole number, 1 or more."),
    ("backup_folders", "/srv/music/backups", '[database] backup_folders must be a list of folders in quotes, for example backup_folders = ["/srv/music/backups"].'),
    ("backup_folders", [3], '[database] backup_folders must be a list of folders in quotes, for example backup_folders = ["/srv/music/backups"].'),
    ("backup_folders", ["ok", ""], '[database] backup_folders must be a list of folders in quotes, for example backup_folders = ["/srv/music/backups"].'),
    ("backup_folders", ["  "], '[database] backup_folders must be a list of folders in quotes, for example backup_folders = ["/srv/music/backups"].'),
])
def test_a_setting_that_is_wrong_is_said_once_and_the_usual_one_is_used(world: SimpleNamespace, caplog: pytest.LogCaptureFixture, field: str, value: object, said: str) -> None:
    setattr(world.ctx.config.database, field, value)
    with caplog.at_level(logging.WARNING, logger="musictoolkit"):
        for _ in range(4):
            assert world.schedule.settings() == backup.Settings()
    assert [r.getMessage() for r in caplog.records] == [f"{said} The usual settings are used."]


def test_settings_that_are_right_are_taken_as_they_are() -> None:
    config = Config()
    config.database.backup_hours = 0.5
    config.database.backup_keep = 2
    config.database.backup_folders = ["/a", "b/c"]
    assert backup.settings_of(config.database) == backup.Settings(0.5, 2, (Path("/a"), Path("b/c")))
    assert backup.settings_of(Config().database) == backup.Settings(24, 7, ()) == backup.Settings()


# ------------------------------------------------------------------------------------------------------------------------ status and health


def test_the_status_says_the_settings_what_each_folder_holds_and_the_last_run(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_folders = [str(world.drive), str(world.tmp / "absent")]
    before = world.schedule.status()
    assert (before["hours"], before["keep"], before["last"], before["overdue"]) == (24, 3, None, False)
    assert [(f["folder"], f["there"], f["copies"], f["size"], f["newest"]) for f in before["folders"]] == [
        (str(world.backups), False, 0, 0, None), (str(world.drive), True, 0, 0, None), (str(world.tmp / "absent"), False, 0, 0, None)]
    world.schedule.run_now()
    after = world.schedule.status()
    made = backup.listing(world.backups)[0]
    assert after["folders"][0]["there"] is True and after["folders"][0]["copies"] == 1 and after["folders"][0]["size"] == made.size > 0
    assert after["folders"][1]["copies"] == 1 and after["folders"][2]["copies"] == 0 and after["folders"][0]["newest"].endswith("+00:00")
    assert after["last"]["problems"] == [{"folder": str(world.tmp / "absent"), "problem": "the folder is not there (is the drive connected?)"}] and after["last"]["when"].endswith("+00:00")


def test_the_copies_are_late_when_the_newest_is_older_than_three_times_the_interval_and_the_app_has_been_up_a_while(world: SimpleNamespace) -> None:
    world.schedule.run_now()
    assert not world.schedule.overdue()
    world.clock.pass_hours(72 - 0.01)
    assert not world.schedule.overdue()
    world.clock.pass_hours(0.02)
    assert world.schedule.overdue() and world.schedule.status()["overdue"] is True


def test_copies_are_late_when_any_folder_has_none_newer_than_three_times_the_interval_and_a_catch_up_ends_it(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_folders = [str(world.drive)]
    world.schedule.run_now()
    for copy in backup.listing(world.drive):
        copy.path.unlink()
    touch(world.drive, "library-20260101-000000.db", 80 * HOUR)  # (the drive has only an old copy; the app's own folder has a new one)
    world.clock.ahead += backup_job.GRACE + 1
    assert world.schedule.overdue() and world.schedule.status()["overdue"] is True
    world.clock.pass_hours(1.1)
    assert world.schedule.tick() is not None  # (the drive is given the new copy)
    assert not world.schedule.overdue()


def test_a_folder_that_is_named_and_not_there_is_late_once_the_app_has_had_its_chance_to_make_a_copy_there(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_folders = [str(world.tmp / "unmounted")]
    world.schedule.run_now()
    assert not world.schedule.overdue()  # (the app was started a moment ago)
    world.clock.ahead += backup_job.GRACE - 1
    assert not world.schedule.overdue()
    world.clock.ahead += 2
    assert world.schedule.overdue()


def test_no_copy_at_all_is_late_only_once_the_app_has_had_a_chance_to_make_the_first(world: SimpleNamespace) -> None:
    assert not world.schedule.overdue()
    world.clock.ahead += backup_job.GRACE - 1
    assert not world.schedule.overdue()
    world.clock.ahead += 2
    assert world.schedule.overdue()


def test_copies_that_are_off_are_never_late(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_hours = 0
    world.clock.pass_hours(10**6)
    assert not world.schedule.overdue()


def test_a_copy_dated_in_the_future_does_not_make_the_copies_look_new(world: SimpleNamespace) -> None:
    touch(world.backups, "library-20990101-000000.db", -86400 * 400)  # (written when the clock was a year ahead)
    world.clock.ahead += backup_job.GRACE + 1
    assert world.schedule.overdue() and world.schedule.tick() is not None  # (so that is as good as none, and one is made)


# -------------------------------------------------------------------------------------------------------------------------------- the thread


def test_the_thread_asks_again_and_again_and_stops_when_asked(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_job, "CHECK_EVERY", 0.01)
    asked = []
    monkeypatch.setattr(world.schedule, "tick", lambda: asked.append(1))
    world.schedule.start()
    thread = world.schedule._thread
    try:
        assert thread is not None and thread.is_alive() and thread.daemon and thread.name == "database-backup"
        world.schedule.start()
        assert world.schedule._thread is thread  # (once)
        deadline = time.time() + 5
        while len(asked) < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert len(asked) >= 3
    finally:
        world.schedule.stop()
    assert not thread.is_alive() and world.schedule._thread is None
    world.schedule.stop()  # (again: nothing happens)


def test_a_look_that_fails_is_logged_and_the_thread_goes_on(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(backup_job, "CHECK_EVERY", 0.01)
    asked = []

    def failing():
        asked.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(world.schedule, "tick", failing)
    with caplog.at_level(logging.WARNING, logger="musictoolkit"):
        world.schedule.start()
        deadline = time.time() + 5
        while len(asked) < 3 and time.time() < deadline:
            time.sleep(0.01)
        world.schedule.stop()
    assert len(asked) >= 3
    said = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(said) >= 3 and all(record.getMessage() == "The scheduled copy of the database failed" for record in said)
    assert all(record.exc_info and isinstance(record.exc_info[1], RuntimeError) and str(record.exc_info[1]) == "boom" for record in said)  # (with what went wrong, for the log)


# --------------------------------------------------------------------------------------------------------------- to the second, and starting again

T = 2_000_000_000.0  # a time that is the clock's, and not the computer's: the copies below are dated as of it


class Fixed:
    """A clock that says what it was told."""

    def __init__(self, now: float = T) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def dated(folder: Path, name: str, when: float) -> Path:
    path = touch(folder, name)
    os.utime(path, (when, when))
    return path


def test_a_copy_is_made_when_the_newest_is_exactly_as_old_as_the_time_between_copies_and_not_a_second_before(world: SimpleNamespace) -> None:
    schedule = BackupSchedule(world.ctx, clock=Fixed())
    copy = dated(world.backups, "library-20330101-000000.db", T - 24 * HOUR + 1)
    assert schedule.tick() is None
    os.utime(copy, (T - 24 * HOUR, T - 24 * HOUR))
    assert schedule.tick() is not None


def test_a_try_is_not_repeated_until_the_time_to_wait_has_passed_to_the_second(world: SimpleNamespace) -> None:
    clock = Fixed()
    schedule = BackupSchedule(world.ctx, clock=clock)
    assert schedule.tick() is not None  # (no copy: one is made, at T)
    for copy in backup.listing(world.backups):
        os.utime(copy.path, (T - 48 * HOUR, T - 48 * HOUR))  # (and it is due again at once, but for the wait)
    clock.now = T + backup_job.RETRY_AFTER - 1
    assert schedule.tick() is None
    clock.now = T + backup_job.RETRY_AFTER
    assert schedule.tick() is not None


def test_the_wait_is_the_time_between_copies_when_that_is_shorter_than_an_hour_to_the_second(world: SimpleNamespace) -> None:
    world.ctx.config.database.backup_hours = 0.25
    clock = Fixed()
    schedule = BackupSchedule(world.ctx, clock=clock)
    assert schedule.tick() is not None
    for copy in backup.listing(world.backups):
        os.utime(copy.path, (T - 48 * HOUR, T - 48 * HOUR))
    clock.now = T + 899
    assert schedule.tick() is None
    clock.now = T + 900
    assert schedule.tick() is not None


def test_copies_are_late_after_ten_minutes_of_running_and_not_at_ten_minutes(world: SimpleNamespace) -> None:
    clock = Fixed()
    schedule = BackupSchedule(world.ctx, clock=clock)  # (started at T)
    clock.now = T + backup_job.GRACE
    assert not schedule.overdue()  # (there is no copy at all; the app has been up exactly the time it is given)
    clock.now = T + backup_job.GRACE + 1
    assert schedule.overdue()


@pytest.mark.parametrize("hours", [24, 0.5])
def test_copies_are_late_when_the_newest_is_more_than_three_times_the_time_between_copies_to_the_second(world: SimpleNamespace, hours: float) -> None:
    world.ctx.config.database.backup_hours = hours
    clock = Fixed()
    schedule = BackupSchedule(world.ctx, clock=clock)
    clock.now = T + 10 * backup_job.GRACE  # (the app has been up a while)
    limit = 3 * hours * 3600
    copy = dated(world.backups, "library-20330101-000000.db", clock.now - limit)
    assert not schedule.overdue()  # (exactly three times)
    os.utime(copy, (clock.now - limit - 1, clock.now - limit - 1))
    assert schedule.overdue()


def test_a_stopped_schedule_can_be_started_again(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_job, "CHECK_EVERY", 0.01)
    asked: list[int] = []
    monkeypatch.setattr(world.schedule, "tick", lambda: asked.append(1))
    world.schedule.start()
    world.schedule.stop()
    before = len(asked)
    world.schedule.start()
    try:
        thread = world.schedule._thread
        assert thread is not None and thread.is_alive()
        deadline = time.time() + 5
        while len(asked) < before + 3 and time.time() < deadline:
            time.sleep(0.01)
        assert len(asked) >= before + 3
    finally:
        world.schedule.stop()


def test_stopping_does_not_wait_for_ever_for_a_copy_that_is_being_made(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_job, "CHECK_EVERY", 0.01)
    monkeypatch.setattr(backup_job, "STOP_WAIT", 0.2)
    inside, release = threading.Event(), threading.Event()

    def stuck() -> None:
        inside.set()
        release.wait(30)

    monkeypatch.setattr(world.schedule, "tick", stuck)
    world.schedule.start()
    try:
        assert inside.wait(5)
        began = time.monotonic()
        world.schedule.stop()
        assert time.monotonic() - began < 5 and world.schedule._thread is None
    finally:
        release.set()
