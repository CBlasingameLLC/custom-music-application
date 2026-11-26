"""Copies of the database in Settings, in a real browser: what the Data card says about them (how often, what each folder holds, whether they are
late), the button, and what a folder that is not there or a copy that failed looks like. The copying itself is tested without a browser."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import expect

from musictoolkit import backup
from musictoolkit.backup import Outcome
from musictoolkit.web import backup_job
from musictoolkit.web.backup_job import BackupSchedule
from tests.e2e.conftest import signed_in_page
from tests.e2e.test_phone import OVERFLOW, PHONE
from tests.test_backup_schedule import Clock

expect.set_options(timeout=10_000)

BUTTON = "Back up the database now"


@pytest.fixture
def schedule(live) -> BackupSchedule:
    """The app's schedule for copies without its thread, so that nothing is made at the minute mark whatever a test is in the middle of."""
    live.ctx.backups.stop()
    live.ctx.backups = BackupSchedule(live.ctx, clock=Clock())
    return live.ctx.backups


def settings(page):
    page.evaluate("location.hash = '#/settings'")
    copies = page.get_by_role("list", name="Copies of the database")
    expect(copies).to_be_visible()
    return copies


def made(live) -> list[str]:
    return [copy.path.name for copy in backup.listing(live.ctx.data_dir / "backups")]


def test_settings_says_how_often_copies_are_made_and_that_none_has_been_yet(page, live, schedule) -> None:
    copies = settings(page)
    expect(copies).to_contain_text("Every 24 hours. The newest 7 are kept in each folder.")
    expect(copies.locator(".diag", has_text="App's folder")).to_contain_text(f"{live.ctx.data_dir / 'backups'}: no copy yet.")
    assert copies.locator(".diag.bad").count() == 0 and made(live) == []


def test_the_button_makes_a_copy_says_where_and_the_list_shows_it(page, live, schedule) -> None:
    copies = settings(page)
    page.get_by_role("button", name=BUTTON).click()
    expect(page.locator(".toast", has_text="Backup saved: ")).to_contain_text(str(live.ctx.data_dir / "backups" / "library-"))
    row = copies.locator(".diag.ok", has_text="App's folder")
    expect(row).to_contain_text("1 copy,")
    expect(row).to_contain_text("The newest was made just now.")
    assert len(made(live)) == 1 and backup.check(live.ctx.data_dir / "backups" / made(live)[0]).ok


def test_a_second_folder_that_is_there_gets_the_copy_too_and_the_toast_says_so(page, live, schedule, tmp_path: Path) -> None:
    drive = tmp_path / "drive" / "backups"
    drive.mkdir(parents=True)
    live.ctx.config.database.backup_folders = [str(drive)]
    copies = settings(page)
    expect(copies.locator(".diag", has_text="Another folder")).to_contain_text(f"{drive}: no copy yet.")
    page.get_by_role("button", name=BUTTON).click()
    expect(page.locator(".toast", has_text="Backup saved in 2 places. The first: ")).to_be_visible()
    expect(copies.locator(".diag.ok", has_text="Another folder")).to_contain_text("1 copy,")
    assert [copy.path.name for copy in backup.listing(drive)] == made(live)


def test_a_folder_that_is_not_there_is_marked_before_and_after_and_the_toast_says_why_only_it_was_missed(page, live, schedule, tmp_path: Path) -> None:
    gone = tmp_path / "unplugged" / "backups"
    live.ctx.config.database.backup_folders = [str(gone)]
    copies = settings(page)
    missing = copies.locator(".diag.bad", has_text="Another folder")
    expect(missing).to_contain_text(f"{gone}: the folder is not there. Is the drive connected?")
    page.get_by_role("button", name=BUTTON).click()
    expect(page.locator(".toast.success", has_text="Backup saved: ")).to_be_visible()  # (the copy that could be made)
    expect(page.locator(".toast.error", has_text="Not saved in ")).to_contain_text(f"{gone}: the folder is not there (is the drive connected?)")
    expect(copies.locator(".diag.ok", has_text="App's folder")).to_contain_text("1 copy,")
    assert copies.locator(".diag.bad").count() == 1  # (the folder's own row says it, and the last copy's trouble is not said a second time)
    assert not gone.exists() and not gone.parent.exists()


def test_a_copy_that_failed_for_another_reason_is_said_in_the_list_as_the_last_copy(page, live, schedule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    live.ctx.config.database.backup_folders = [str(drive)]
    real = backup.run

    def drive_is_full(db_path, primary, others=(), keep=7, now=None):
        outcomes = real(db_path, primary, [], keep, now)
        return outcomes + [Outcome(drive, problem="No space left on device")]

    monkeypatch.setattr(backup, "run", drive_is_full)
    copies = settings(page)
    page.get_by_role("button", name=BUTTON).click()
    expect(page.locator(".toast.error", has_text="Not saved in ")).to_contain_text(f"{drive}: No space left on device")
    expect(copies.locator(".diag.bad", has_text="Last copy")).to_contain_text(f"{drive}: No space left on device")
    expect(copies.locator(".diag.ok", has_text="App's folder")).to_contain_text("1 copy,")


def test_when_no_copy_could_be_made_the_page_says_why_and_the_list_stays(page, live, schedule, monkeypatch: pytest.MonkeyPatch) -> None:
    page.allowed_statuses.add(500)  # (the refusal is what is being looked at)
    monkeypatch.setattr(backup, "run", lambda db_path, primary, *args, **kwargs: [Outcome(primary, problem="Permission denied")])
    copies = settings(page)
    page.get_by_role("button", name=BUTTON).click()
    expect(page.locator(".toast.error")).to_contain_text("No copy was made: Permission denied")
    expect(copies.locator(".diag.bad", has_text="Last copy")).to_contain_text("Permission denied")


def test_copies_that_are_late_are_marked(page, live, schedule) -> None:
    schedule._clock.ahead = backup_job.GRACE + 1  # (the app has been up ten minutes and has made none)
    copies = settings(page)
    late = copies.locator(".diag.bad", has_text="Late")
    expect(late).to_contain_text("no copy newer than three times the time between copies")
    page.get_by_role("button", name=BUTTON).click()
    expect(copies.locator(".diag.bad", has_text="Late")).to_have_count(0)


def test_copies_that_are_off_say_that_the_button_is_the_only_way_and_are_never_late(page, live, schedule) -> None:
    live.ctx.config.database.backup_hours = 0
    schedule._clock.ahead = 10**7
    copies = settings(page)
    expect(copies).to_contain_text("Off: a copy is made only when you press the button. The newest 7 are kept in each folder.")
    assert copies.locator(".diag.bad").count() == 0
    page.get_by_role("button", name=BUTTON).click()
    expect(copies.locator(".diag.ok", has_text="App's folder")).to_contain_text("1 copy,")


def test_every_hour_and_a_setting_that_is_wrong_are_said_plainly(page, live, schedule) -> None:
    live.ctx.config.database.backup_hours = 1
    expect(settings(page)).to_contain_text("Every hour. The newest 7")
    live.ctx.config.database.backup_hours = "daily"  # (the server would not have started; the page still shows something true: the usual settings)
    page.evaluate("location.hash = '#/'")
    expect(settings(page)).to_contain_text("Every 24 hours. The newest 7")


def test_a_long_folder_name_does_not_make_the_page_wider_than_a_phone(browser, live, schedule) -> None:
    long = "/srv/music/" + "a-folder-with-a-very-long-name-" * 6 + "backups"
    live.ctx.config.database.backup_folders = [long]
    pages = signed_in_page(browser, live, device=PHONE)
    page = next(pages)
    copies = settings(page)
    expect(copies).to_contain_text(long)
    page.wait_for_timeout(250)
    got = page.evaluate(OVERFLOW)
    assert got["window"] == 390 and got["scroll"] <= got["view"] and got["page"] <= 390 and got["offenders"] == [], got
    next(pages, None)
