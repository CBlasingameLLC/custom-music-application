from pathlib import Path
from typing import Optional

import typer

from musictoolkit.config import Config, load_config
from musictoolkit.db.connection import connect
from musictoolkit.ingest import dedupe as dedupe_ops
from musictoolkit.ingest import organizer, scanner, tagger
from musictoolkit.integrations import musicbrainz_client
from musictoolkit.logging_setup import setup_logging

app = typer.Typer(
    name="mtk",
    help="Personal music library organizer, importer, recommender, and device sync toolkit.",
    no_args_is_help=True,
)

state: dict[str, object] = {"config_path": "config.toml", "db_path": None, "verbose": False, "config": Config()}


@app.callback()
def main(
    config: str = typer.Option("config.toml", "--config", help="Path to config.toml"),
    db: Optional[str] = typer.Option(None, "--db", help="Override database path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    cfg = load_config(config)
    state["config_path"] = config
    state["db_path"] = db
    state["verbose"] = verbose
    state["config"] = cfg
    setup_logging(cfg.logging.dir, "DEBUG" if verbose else cfg.logging.level)


def _connection():
    cfg: Config = state["config"]  # type: ignore[assignment]
    db_path = state["db_path"] or cfg.database.path
    return connect(db_path)


def _configure_musicbrainz() -> None:
    cfg: Config = state["config"]  # type: ignore[assignment]
    musicbrainz_client.configure(cfg.musicbrainz.app_name, cfg.musicbrainz.app_version, cfg.musicbrainz.contact)


def _not_implemented(command: str, phase: str) -> None:
    typer.echo(f"'{command}' is not implemented yet — arriving in {phase}.")
    raise typer.Exit(code=1)


@app.command()
def scan(path: str = typer.Argument(..., help="Library root to scan")) -> None:
    """Scan a library root and populate the tracks table."""
    conn = _connection()
    result = scanner.scan_library(conn, Path(path))
    conn.close()
    typer.echo(
        f"Added: {result.added}  Updated: {result.updated}  Unchanged: {result.unchanged}  "
        f"Missing: {result.missing}  Errors: {result.errors}"
    )


@app.command()
def tag(
    path: str = typer.Argument(..., help="Library root to enrich tags for"),
    apply: bool = typer.Option(False, "--apply", help="Write changes; default is dry-run"),
) -> None:
    """Enrich sparse tags via MusicBrainz lookups."""
    conn = _connection()
    _configure_musicbrainz()
    result = tagger.propose_tags(conn, Path(path))

    typer.echo(f"Proposed: {len(result.proposals)}  No match: {result.skipped_no_match}  Errors: {result.errors}")
    for p in result.proposals:
        typer.echo(f"  {p.file_path}")
        typer.echo(
            f"    -> title={p.proposed.get('title')!r} artist={p.proposed.get('artist')!r} "
            f"album={p.proposed.get('album')!r} (confidence={p.mb_confidence:.2f})"
        )

    if apply:
        tagger.apply_tags(conn, result.proposals)
        typer.echo(f"Applied {len(result.proposals)} tag updates.")
    elif result.proposals:
        typer.echo("Dry run — pass --apply to write these changes.")
    conn.close()


@app.command()
def organize(
    path: str = typer.Argument(..., help="Library root to organize"),
    apply: bool = typer.Option(False, "--apply", help="Write changes; default is dry-run"),
) -> None:
    """Move/rename files into the canonical folder scheme."""
    cfg: Config = state["config"]  # type: ignore[assignment]
    conn = _connection()
    result = organizer.propose_organization(conn, Path(path), cfg.library.canonical_scheme)

    typer.echo(
        f"To move: {len(result.proposals)}  Unchanged: {result.unchanged}  Collisions: {len(result.collisions)}"
    )
    for p in result.proposals:
        typer.echo(f"  {p.old_path} -> {p.new_path}")
    for old, new in result.collisions:
        typer.echo(f"  COLLISION (skipped): {old} -> {new}")

    if apply:
        moved = organizer.apply_organization(conn, result.proposals)
        typer.echo(f"Moved {moved} files.")
    elif result.proposals:
        typer.echo("Dry run — pass --apply to write these changes.")
    conn.close()


@app.command()
def dedupe(
    use_content_hash: bool = typer.Option(
        False, "--content-hash", help="Also compare by content hash (slower, most thorough)"
    ),
    apply: bool = typer.Option(False, "--apply", help="Quarantine losers; default is dry-run"),
) -> None:
    """Detect likely-duplicate tracks."""
    cfg: Config = state["config"]  # type: ignore[assignment]
    conn = _connection()
    library_root = Path(cfg.library.roots[0]) if cfg.library.roots else Path(".")
    groups = dedupe_ops.find_duplicate_groups(conn, library_root, use_content_hash)

    typer.echo(f"Duplicate groups found: {len(groups)}")
    for group in groups:
        typer.echo(f"  [{group.reason}] {group.key}")
        for file_path in group.file_paths:
            typer.echo(f"    {file_path}")

    if apply:
        moved = dedupe_ops.apply_quarantine(conn, groups, library_root)
        typer.echo(f"Quarantined {moved} duplicate files into _duplicates_review/.")
    elif groups:
        typer.echo("Dry run — pass --apply to quarantine losers into _duplicates_review/.")
    conn.close()


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
