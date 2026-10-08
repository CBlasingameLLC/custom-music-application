"""PyInstaller entry point — re-exposes the existing Typer CLI unchanged.

PyInstaller freezes a script, not a package reference, so this thin wrapper
is what gets frozen into mtk-backend.exe. Every subcommand (scan, tag,
organize, dedupe, devices, import-playlist, sync, import-spotify,
recommend, review, dashboard) behaves identically to running `mtk` from a
source checkout.
"""

from musictoolkit.cli import app

if __name__ == "__main__":
    app()
