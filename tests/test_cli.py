from typer.testing import CliRunner

from musictoolkit.cli import app

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
    result = runner.invoke(app, ["review"])
    assert result.exit_code == 1
    assert "not implemented yet" in result.output
    assert "Phase 4" in result.output
