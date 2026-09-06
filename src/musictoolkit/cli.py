import shutil
import tempfile
from pathlib import Path
from typing import Optional

import typer

from musictoolkit.config import Config, load_config
from musictoolkit.db.connection import connect
from musictoolkit.history import spotify_import
from musictoolkit.ingest import dedupe as dedupe_ops
from musictoolkit.ingest import organizer, scanner, tagger
from musictoolkit.integrations import lastfm_client, musicbrainz_client
from musictoolkit.logging_setup import setup_logging
from musictoolkit.recommend import engine, review_queue
from musictoolkit.sync import device_detect, mirror, playlist_import
from musictoolkit.sync import selector as selector_ops

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
    candidates = device_detect.list_candidate_devices()
    if not candidates:
        typer.echo("No mounted volumes detected.")
        return
    for c in candidates:
        marker = "*" if c.likely_removable else " "
        typer.echo(
            f"{marker} {c.mountpoint}  ({c.fstype})  free: {c.free_bytes / 1e9:.2f} GB / {c.total_bytes / 1e9:.2f} GB"
        )
    typer.echo("(* = looks removable — always verify this is the right device before syncing)")


@app.command(name="import-playlist")
def import_playlist_cmd(
    path: str = typer.Argument(..., help="Path to an M3U/M3U8 playlist file"),
    name: Optional[str] = typer.Option(None, "--name", help="Playlist name (default: filename)"),
) -> None:
    """Import an M3U/M3U8 playlist, matching entries to already-scanned tracks."""
    conn = _connection()
    matched = playlist_import.import_playlist(conn, Path(path), name)
    typer.echo(f"Matched {matched} entries to existing tracks.")
    conn.close()


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
    cfg: Config = state["config"]  # type: ignore[assignment]
    device_root = Path(target)
    if not device_root.exists():
        typer.echo(f"Target path does not exist: {target}")
        raise typer.Exit(code=1)
    device_root = device_root.resolve()

    conn = _connection()
    try:
        selected = selector_ops.resolve_selection(conn, playlist, tag, min_rating, all_)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)

    device_id = mirror.get_or_create_device(conn, str(device_root))
    free_bytes = shutil.disk_usage(device_root).free
    plan = mirror.plan_sync(conn, device_id, device_root, selected, cfg.sync.device_scheme, free_bytes)

    typer.echo(
        f"To copy: {len(plan.to_copy)}  Unchanged: {plan.unchanged}  "
        f"To prune: {len(plan.to_prune) if prune else 0} (found: {len(plan.to_prune)})  "
        f"Size: {plan.total_bytes_to_copy / 1e9:.3f} GB  Free: {plan.free_bytes_on_device / 1e9:.3f} GB"
    )

    if not mirror.has_sufficient_space(plan):
        typer.echo("Not enough free space on the device for this selection. Aborting.")
        conn.close()
        raise typer.Exit(code=1)

    if apply:
        copied, pruned = mirror.apply_sync(conn, device_id, device_root, plan, prune)
        suffix = f" Pruned {pruned} files." if prune else ""
        typer.echo(f"Copied {copied} files.{suffix}")
    else:
        typer.echo("Dry run — pass --apply to write these changes (add --prune to also remove deselected files).")
    conn.close()


@app.command(name="import-spotify")
def import_spotify(
    zip_or_folder: str = typer.Argument(..., help="Path to the Spotify extended streaming history export"),
    submit_listenbrainz: bool = typer.Option(
        False, "--submit-listenbrainz", help="Backfill imported listens to ListenBrainz"
    ),
) -> None:
    """Import Spotify's Extended Streaming History export."""
    cfg: Config = state["config"]  # type: ignore[assignment]
    conn = _connection()

    with tempfile.TemporaryDirectory() as tmp:
        try:
            summary = spotify_import.import_history(conn, Path(zip_or_folder), Path(tmp))
        except ValueError as exc:
            typer.echo(str(exc))
            conn.close()
            raise typer.Exit(code=1)

    typer.echo(
        f"Rows seen: {summary.total_rows_seen}  Music: {summary.music_rows}  "
        f"Podcast/other skipped: {summary.podcast_rows_skipped}"
    )
    if summary.date_range:
        typer.echo(f"Date range: {summary.date_range[0]} to {summary.date_range[1]}")
    if summary.top_artists:
        typer.echo("Top artists by play count:")
        for artist, count in summary.top_artists:
            typer.echo(f"  {artist}: {count}")
    typer.echo(
        f"Inserted: {summary.inserted}  Already imported: {summary.duplicates_skipped}  "
        f"Matched to library: {summary.matched_to_library}"
    )

    if submit_listenbrainz:
        if not cfg.listenbrainz.user_token:
            typer.echo("No ListenBrainz user token configured — set listenbrainz.user_token in config.toml first.")
            conn.close()
            raise typer.Exit(code=1)
        submitted = spotify_import.backfill_to_listenbrainz(conn, cfg.listenbrainz.user_token)
        typer.echo(f"Submitted {submitted} listens to ListenBrainz.")

    conn.close()


@app.command()
def recommend(
    source: str = typer.Option("listenbrainz", help="listenbrainz | lastfm | both"),
    limit: int = typer.Option(20, help="Max recommendations to fetch"),
) -> None:
    """Fetch new-music recommendations not already in the library."""
    cfg: Config = state["config"]  # type: ignore[assignment]
    conn = _connection()
    candidates: list[engine.Candidate] = []

    if source in ("listenbrainz", "both"):
        if not cfg.listenbrainz.enabled:
            typer.echo("ListenBrainz is disabled in config.")
        elif not cfg.listenbrainz.username:
            typer.echo("No ListenBrainz username configured — set listenbrainz.username in config.toml.")
        else:
            _configure_musicbrainz()
            try:
                candidates.extend(
                    engine.fetch_listenbrainz_candidates(
                        cfg.listenbrainz.username, cfg.listenbrainz.user_token or None, limit
                    )
                )
            except Exception as exc:
                typer.echo(f"ListenBrainz fetch failed: {exc}")

    if source in ("lastfm", "both"):
        if not cfg.lastfm.enabled:
            typer.echo("Last.fm is disabled in config (it's secondary/optional by default).")
        elif not cfg.lastfm.api_key:
            typer.echo("No Last.fm API key configured — set lastfm.api_key in config.toml.")
        else:
            lastfm_client.configure(cfg.lastfm.api_key, cfg.lastfm.api_secret)
            seed_artists = engine.top_played_artists(conn, limit=10)
            try:
                candidates.extend(engine.fetch_lastfm_candidates(seed_artists, limit_per_artist=5))
            except Exception as exc:
                typer.echo(f"Last.fm fetch failed: {exc}")

    new_candidates = engine.filter_owned(conn, candidates)
    weights = engine.compute_play_history_weights(conn)
    ranked = engine.rank(new_candidates, weights)
    saved = engine.save_recommendations(conn, ranked)

    typer.echo(
        f"Fetched: {len(candidates)}  Already owned (filtered): {len(candidates) - len(new_candidates)}  "
        f"New: {len(new_candidates)}  Saved: {saved}"
    )
    for c in ranked[:limit]:
        label = c.artist_name + (f" - {c.track_name}" if c.track_name else "")
        typer.echo(f"  [{c.source}] {label}  (score={c.score:.2f})  {c.reason}")

    conn.close()


@app.command()
def review() -> None:
    """Triage pending recommendations (owned / dismissed / accepted)."""
    conn = _connection()
    pending = review_queue.list_pending(conn)
    if not pending:
        typer.echo("No pending recommendations. Run `mtk recommend` first.")
        conn.close()
        return

    for row in pending:
        label = row["artist_name"] + (f" - {row['track_name']}" if row["track_name"] else "")
        typer.echo(f"\n[{row['id']}] {label}  (source={row['source']}, score={row['score']:.2f})")
        typer.echo(f"    {row['reason']}")
        choice = typer.prompt("  (o)wned / (d)ismiss / (a)ccept / (s)kip", default="s")
        if choice.lower().startswith("o"):
            review_queue.set_status(conn, row["id"], "owned")
        elif choice.lower().startswith("d"):
            review_queue.set_status(conn, row["id"], "dismissed")
        elif choice.lower().startswith("a"):
            review_queue.set_status(conn, row["id"], "accepted")
    conn.close()


@app.command()
def dashboard(port: int = typer.Option(4533, "--port", help="Port to bind on 127.0.0.1")) -> None:
    """Launch the optional local-only web dashboard (127.0.0.1 only)."""
    # Imported lazily: FastAPI/uvicorn are only needed for this one optional
    # command, so every other subcommand stays fast to start and doesn't
    # require them to even be installed.
    import uvicorn

    from musictoolkit.dashboard import app as dashboard_module

    cfg: Config = state["config"]  # type: ignore[assignment]
    db_path = state["db_path"] or cfg.database.path
    dashboard_module.configure(Path(db_path))
    typer.echo(f"Starting dashboard at http://127.0.0.1:{port} (Ctrl+C to stop)")
    uvicorn.run(dashboard_module.app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    app()
