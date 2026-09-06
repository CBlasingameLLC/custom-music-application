from typing import Optional

import typer

app = typer.Typer(
    name="mtk",
    help="Personal music library organizer, importer, recommender, and device sync toolkit.",
    no_args_is_help=True,
)

state: dict[str, object] = {"config_path": "config.toml", "db_path": None, "verbose": False}


@app.callback()
def main(
    config: str = typer.Option("config.toml", "--config", help="Path to config.toml"),
    db: Optional[str] = typer.Option(None, "--db", help="Override database path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    state["config_path"] = config
    state["db_path"] = db
    state["verbose"] = verbose


def _not_implemented(command: str, phase: str) -> None:
    typer.echo(f"'{command}' is not implemented yet — arriving in {phase}.")
    raise typer.Exit(code=1)


@app.command()
def scan(path: str = typer.Argument(..., help="Library root to scan")) -> None:
    """Scan a library root and populate the tracks table."""
    _not_implemented("scan", "Phase 1")


@app.command()
def tag(
    path: str = typer.Argument(..., help="Library root to enrich tags for"),
    apply: bool = typer.Option(False, "--apply", help="Write changes; default is dry-run"),
) -> None:
    """Enrich sparse tags via MusicBrainz lookups."""
    _not_implemented("tag", "Phase 1")


@app.command()
def organize(
    path: str = typer.Argument(..., help="Library root to organize"),
    apply: bool = typer.Option(False, "--apply", help="Write changes; default is dry-run"),
) -> None:
    """Move/rename files into the canonical folder scheme."""
    _not_implemented("organize", "Phase 1")


@app.command()
def dedupe(
    apply: bool = typer.Option(False, "--apply", help="Quarantine losers; default is dry-run"),
) -> None:
    """Detect likely-duplicate tracks."""
    _not_implemented("dedupe", "Phase 1")


@app.command()
def devices() -> None:
    """List detected removable volumes."""
    _not_implemented("devices", "Phase 2")


@app.command()
def sync(
    target: str = typer.Argument(..., help="Mount path of the device to sync to"),
    playlist: Optional[str] = typer.Option(None, help="Sync only this playlist"),
    tag: Optional[str] = typer.Option(None, help="Sync only tracks matching this tag/genre"),
    min_rating: Optional[int] = typer.Option(None, help="Sync only tracks at/above this rating"),
    all_: bool = typer.Option(False, "--all", help="Sync the entire library"),
    apply: bool = typer.Option(False, "--apply", help="Write changes; default is dry-run"),
    prune: bool = typer.Option(False, "--prune", help="Remove on-device files no longer selected"),
) -> None:
    """Sync a selected subset of the library onto a device."""
    _not_implemented("sync", "Phase 2")


@app.command(name="import-spotify")
def import_spotify(
    zip_or_folder: str = typer.Argument(..., help="Path to the Spotify extended streaming history export"),
    submit_listenbrainz: bool = typer.Option(
        False, "--submit-listenbrainz", help="Backfill imported listens to ListenBrainz"
    ),
) -> None:
    """Import Spotify's Extended Streaming History export."""
    _not_implemented("import-spotify", "Phase 3")


@app.command()
def recommend(
    source: str = typer.Option("listenbrainz", help="listenbrainz | lastfm | both"),
    limit: int = typer.Option(20, help="Max recommendations to fetch"),
) -> None:
    """Fetch new-music recommendations not already in the library."""
    _not_implemented("recommend", "Phase 4")


@app.command()
def review() -> None:
    """Triage pending recommendations (owned / dismissed / accepted)."""
    _not_implemented("review", "Phase 4")


@app.command()
def dashboard() -> None:
    """Launch the optional local-only web dashboard (127.0.0.1 only)."""
    _not_implemented("dashboard", "Phase 5")


if __name__ == "__main__":
    app()
