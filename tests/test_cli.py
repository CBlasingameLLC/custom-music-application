from pathlib import Path

from typer.testing import CliRunner

from musictoolkit.cli import app
from musictoolkit.db.connection import connect

runner = CliRunner()

EXPECTED_COMMANDS = [
    "scan",
    "tag",
    "organize",
    "dedupe",
    "devices",
    "import-playlist",
    "sync",
    "import-spotify",
    "recommend",
    "review",
    "dashboard",
]


def test_help_lists_all_stubbed_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in EXPECTED_COMMANDS:
        assert command in result.output


def test_unimplemented_command_exits_nonzero_with_message() -> None:
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 1
    assert "not implemented yet" in result.output
    assert "Phase 5" in result.output


def test_review_command_applies_chosen_statuses(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO recommendations (artist_name, track_name, source, score, status) "
        "VALUES ('Artist One', 'Song One', 'listenbrainz_cf', 0.9, 'new')"
    )
    conn.execute(
        "INSERT INTO recommendations (artist_name, track_name, source, score, status) "
        "VALUES ('Artist Two', 'Song Two', 'listenbrainz_cf', 0.5, 'new')"
    )
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["--db", str(db_path), "review"], input="accept\ndismiss\n")
    assert result.exit_code == 0

    conn2 = connect(db_path)
    row1 = conn2.execute("SELECT status FROM recommendations WHERE artist_name = 'Artist One'").fetchone()
    row2 = conn2.execute("SELECT status FROM recommendations WHERE artist_name = 'Artist Two'").fetchone()
    assert row1["status"] == "accepted"
    assert row2["status"] == "dismissed"
    conn2.close()
