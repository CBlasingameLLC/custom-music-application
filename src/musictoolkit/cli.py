import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

import typer

from musictoolkit import server as server_ops
from musictoolkit.config import Config, bootstrap_if_missing, load_config, resolve_config_path, save_config
from musictoolkit.db.connection import connect
from musictoolkit.history import spotify_import
from musictoolkit.ingest import dedupe as dedupe_ops
from musictoolkit.ingest import organizer, reroot, scanner, tagger
from musictoolkit.integrations import lastfm_client, musicbrainz_client
from musictoolkit.logging_setup import setup_logging
from musictoolkit.recommend import review_queue
from musictoolkit.recommend import service as recommend_service
from musictoolkit.sync import device_detect, mirror, playlist_import
from musictoolkit.sync import selector as selector_ops

app = typer.Typer(
    name="mtk",
    help="Personal music library organizer, importer, recommender, and device sync toolkit.",
    no_args_is_help=True,
)

state: dict[str, object] = {"config_path": "config.toml", "db_path": None, "verbose": False, "config": Config()}


def _find_example_config() -> Optional[Path]:
    """Locate config.example.toml for the first-run bootstrap — works both
    from a source checkout and from inside a PyInstaller onefile bundle,
    where bundled data files are extracted to a temp dir at sys._MEIPASS."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidate = Path(sys._MEIPASS) / "config.example.toml"
    else:
        candidate = Path(__file__).resolve().parent.parent.parent / "config.example.toml"
    return candidate if candidate.exists() else None


@app.callback()
def main(
    config: Optional[str] = typer.Option(
        None, "--config", help="Path to config.toml (default: ./config.toml if present, else ~/.musictoolkit/config.toml)"
    ),
    db: Optional[str] = typer.Option(None, "--db", help="Override database path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    resolved_config_path = resolve_config_path(config)
    bootstrap_if_missing(resolved_config_path, example_path=_find_example_config())
    cfg = load_config(resolved_config_path)
    state["config_path"] = str(resolved_config_path)
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
def scan(
    path: str = typer.Argument(..., help="Library root to scan"),
    force: bool = typer.Option(
        False, "--force", help="Scan even when the folder is missing or empty while the library knows songs in it (every one of them is then marked missing)"
    ),
) -> None:
    """Scan a library root and populate the tracks table."""
    conn = _connection()
    try:
        result = scanner.scan_library(conn, Path(path), force=force)
    except scanner.LibraryUnavailable as error:
        typer.echo(f"Not scanned: {error}", err=True)
        raise typer.Exit(1) from None
    finally:
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
        moved = organizer.apply_organization(conn, result.proposals, Path(path))
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
    result = recommend_service.refresh(conn, cfg, source, limit)
    for message in result.messages:
        typer.echo(message)
    typer.echo(
        f"Fetched: {result.fetched}  Already owned (filtered): {result.already_owned}  "
        f"New: {result.new}  Saved: {result.saved}"
    )
    for c in result.top:
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


def _is_terminal() -> bool:
    """Is this command's output being read by a person at a terminal (and not written to a file, a pipe or a service's journal)?"""
    return sys.stdout.isatty()


@app.command()
def dashboard(port: int = typer.Option(4533, "--port", help="Port to bind on 127.0.0.1")) -> None:
    """Run the Music Toolkit app's local server (127.0.0.1 only) and print its address."""
    # Imported lazily: FastAPI/uvicorn are only needed for this command, so every
    # other subcommand stays fast to start.
    import os
    import secrets

    import uvicorn

    from musictoolkit.web.app import create_app

    cfg: Config = state["config"]  # type: ignore[assignment]
    db_path = state["db_path"] or cfg.database.path
    # The desktop app supplies its own token (MTK_TOKEN) so it can open the window; a bare `mtk dashboard` makes one.
    given = os.environ.get("MTK_TOKEN")
    token = given or secrets.token_urlsafe(24)
    web_app = create_app(
        db_path=Path(str(db_path)),
        config_path=Path(str(state["config_path"])),
        config=cfg,
        token=token,
        rescan_on_start=True,
    )
    # The link that signs in holds the token, so it goes to a person at a terminal and nowhere else: output that is not a terminal is a
    # log (the desktop app's log file, a service's journal), and a token written to a log has leaked.
    if given:
        typer.echo(f"Music Toolkit is running on 127.0.0.1:{port}  (sign in with the token in MTK_TOKEN)")
    elif _is_terminal():
        typer.echo(f"Music Toolkit is running at http://127.0.0.1:{port}/?token={token}  (Ctrl+C to stop)")
    else:
        typer.echo(
            f"Music Toolkit is running on 127.0.0.1:{port}, but nobody can sign in: it made its own token, which is shown only at a terminal. "
            "Set MTK_TOKEN to a token of your own, or use `mtk serve`.",
            err=True,
        )
    uvicorn.run(web_app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


def _data_dir() -> Path:
    """The folder the config file is in: the token file, the database (unless told otherwise), the logs and the caches live there."""
    return Path(str(state["config_path"])).absolute().parent


@app.command()
def serve(
    host: Optional[str] = typer.Option(None, "--host", help="Address to listen on (default: [server] host in config.toml, 127.0.0.1)"),
    port: Optional[int] = typer.Option(None, "--port", help="Port to listen on (default: [server] port, 4533)"),
) -> None:
    """Run the app as a server that other devices use (a Raspberry Pi at home, say), until it is stopped.

    It refuses to listen beyond this computer unless [server] allowed_hosts in config.toml names every name the devices reach it by.
    The token is MTK_TOKEN, or the file server.token next to config.toml (made at the first start); `mtk token` shows it."""
    import uvicorn

    from musictoolkit.web.app import create_app

    cfg: Config = state["config"]  # type: ignore[assignment]
    settings = cfg.server
    bind = (host or settings.host).strip()
    try:
        number = server_ops.check_port(port if port is not None else settings.port)
        names = server_ops.allowed_names(bind, settings.allowed_hosts)
        logins = server_ops.tailscale_logins(bind, settings.tailscale_logins)
        token, source = server_ops.server_token(_data_dir(), os.environ)
    except server_ops.ServeError as error:
        typer.echo(f"Cannot start the server: {error}", err=True)
        raise typer.Exit(2) from None
    db_path = state["db_path"] or cfg.database.path
    web_app = create_app(
        db_path=Path(str(db_path)),
        config_path=Path(str(state["config_path"])),
        config=cfg,
        token=token,
        rescan_on_start=True,
        allowed_hosts=names,
        login_days=settings.login_days,
        rescan_hours=settings.rescan_hours,
        tailscale_logins=logins,
    )
    typer.echo(f"Music Toolkit server listening on {bind}:{number}")
    typer.echo(f"  answers to the names: {', '.join(['127.0.0.1', 'localhost', *names])}")
    typer.echo(f"  token: {source}  (the link that signs a device in: mtk token --url https://NAME)")
    if logins:  # (how many, not who: a log is not the place for people's accounts)
        typer.echo(f"  signs in {len(logins)} Tailscale account{'s' if len(logins) != 1 else ''} without the token  (behind `tailscale serve`)")
    typer.echo(f"  data folder: {_data_dir()}")
    if (warning := server_ops.token_file_warning(_data_dir())) and source != "the MTK_TOKEN environment variable":
        typer.echo(f"  WARNING: {warning}", err=True)
    if not server_ops.is_loopback_bind(bind):
        typer.echo(
            "  WARNING: anyone on this network who has the token can use everything in the app, and there are no accounts. "
            "Never forward a port from the internet to it; reach it from outside through a private network such as Tailscale.",
            err=True,
        )
    uvicorn.run(web_app, host=bind, port=number, log_level="warning", access_log=False, proxy_headers=True, forwarded_allow_ips="127.0.0.1")


@app.command()
def token(
    url: Optional[str] = typer.Option(
        None, "--url", help="How devices reach this server (https://musicpi.example.ts.net): print the link that signs a device in instead of the bare token"
    ),
) -> None:
    """Show the server's token, or with --url the link to open once on each device. Anyone who has it can use the app."""
    try:
        value, _ = server_ops.server_token(_data_dir(), os.environ, create=False)
        typer.echo(server_ops.sign_in_link(url, value) if url else value)
    except server_ops.ServeError as error:
        typer.echo(f"{error}", err=True)
        raise typer.Exit(1) from None


library_app = typer.Typer(name="library", help="Look after the library's database.", no_args_is_help=True)
app.add_typer(library_app, name="library")


@library_app.command("reroot")
def library_reroot(
    from_prefix: str = typer.Option(..., "--from", help=r"The folder the library was in, as the database has it (D:\Music)"),
    to_prefix: str = typer.Option(..., "--to", help="The folder it is in now (/srv/music/library)"),
    apply: bool = typer.Option(False, "--apply", help="Change the database; default is a dry run that only says what would change"),
    skip_conflicts: bool = typer.Option(False, "--skip-conflicts", help="Leave alone the songs whose new path another song already has, instead of stopping"),
    update_config: bool = typer.Option(False, "--update-config", help="Also change the library folders (and the rip folder) in config.toml"),
) -> None:
    """Tell the database where the library is now, after it was copied somewhere else (a Windows PC's D:\\Music to /srv/music/library).

    Every song keeps its number, rating, plays and playlists. Stop the app first. A copy of the database is kept before anything changes."""
    cfg: Config = state["config"]  # type: ignore[assignment]
    try:
        mapping = reroot.Mapping.of(from_prefix, to_prefix)
    except reroot.RerootError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from None
    conn = _connection()
    try:
        plan = reroot.plan_reroot(conn, mapping)
        where = reroot.song_folders(conn) if plan.songs_outside else []
    finally:
        conn.close()
    typer.echo(f"{'Re-rooting' if apply else 'Dry run of re-rooting'} {mapping.source} -> {mapping.target}")
    for (table, column), count in plan.counts.items():
        typer.echo(f"  {table}.{column}: {count:,}")
    songs_there = plan.counts.get(("tracks", "file_path"), 0) + len(plan.conflicts)
    typer.echo(f"  songs under the old folder: {songs_there:,} of {plan.songs:,}  (outside it, left alone: {plan.songs_outside:,})")
    if where:  # (--from has to be written the way the database has it, which is not always the way a person remembers it)
        typer.echo("  the database has its songs in: " + ", ".join(f"{folder} ({count:,})" for folder, count in where))
    if not songs_there:  # (the histories of a library with no song in the folder are not what anyone means to move)
        typer.echo(f"No song is in {mapping.source}, so there is nothing to change." + (" Use one of the folders above as --from." if where else ""), err=True)
        raise typer.Exit(1)
    for old, new in plan.examples():
        typer.echo(f"  e.g. {old}  ->  {new}")
    folders = reroot.map_folders([*cfg.library.roots, cfg.disc.rip_root], mapping)
    for old, new in folders:
        typer.echo(f"  config folder: {old}  ->  {new}" + ("" if update_config else "   (use --update-config, or Settings, to change it)"))
    if plan.conflicts:
        typer.echo(f"  {len(plan.conflicts):,} songs would get a path that another song already has, for example {plan.conflicts[0][0]} -> {plan.conflicts[0][1]}", err=True)
    if not apply:
        typer.echo("Nothing was changed. Add --apply to make the change (a copy of the database is kept first).")
        raise typer.Exit(1 if plan.conflicts and not skip_conflicts else 0)
    db_path = Path(str(state["db_path"] or cfg.database.path))
    try:
        copy = reroot.apply_reroot(db_path, plan, skip_conflicts=skip_conflicts)
    except reroot.RerootConflict as error:
        typer.echo(f"Nothing was changed: {error} Look at them, or use --skip-conflicts to leave exactly those songs as they are.", err=True)
        raise typer.Exit(1) from None
    if update_config and folders:
        mapped = dict(folders)
        cfg.library.roots = [mapped.get(root, root) for root in cfg.library.roots]
        cfg.disc.rip_root = mapped.get(cfg.disc.rip_root, cfg.disc.rip_root)
        save_config(str(state["config_path"]), cfg)
        typer.echo("  config.toml now names the new folders (comments in it were not kept).")
    typer.echo(f"Done: {len(plan.changes):,} paths now point to {mapping.target}. The database as it was is kept in {copy}.")
    typer.echo(f"Next: make sure [library] roots names {mapping.target}, then run `mtk scan {mapping.target}`: songs whose size and time are unchanged are reported as Unchanged.")


if __name__ == "__main__":
    app()
