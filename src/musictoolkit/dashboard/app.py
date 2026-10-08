from __future__ import annotations

import html
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from musictoolkit.db.connection import connect
from musictoolkit.ingest import scanner
from musictoolkit.recommend import review_queue

logger = logging.getLogger("musictoolkit")

app = FastAPI(title="Personal Music Toolkit Dashboard")

_db_path: Path | None = None
_log_dir: Path | None = None


def configure(db_path: Path, log_dir: Path | None = None) -> None:
    """Must be called once before serving requests — set by `mtk dashboard`
    to whichever DB the rest of the CLI is already using.

    Opens the DB through connect() once so a first launch (no data directory
    or database yet — exactly what a freshly installed desktop app sees)
    creates it and applies migrations, instead of every page failing."""
    global _db_path, _log_dir
    _db_path = db_path
    _log_dir = log_dir
    connect(db_path).close()


def _connection() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("dashboard.configure(db_path) must be called before serving requests")
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _page(title: str, body: str, head_extra: str = "") -> str:
    return f"""<!doctype html>
<html>
<head>
<title>{html.escape(title)}</title>
{head_extra}
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  nav a {{ margin-right: 1rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; }}
  .bar-row {{ display: flex; align-items: center; margin: 0.3rem 0; }}
  .bar {{ background: #4a6fa5; height: 1rem; margin-right: 0.5rem; }}
  form {{ display: inline; }}
  button {{ margin-right: 0.3rem; }}
</style>
</head>
<body>
<nav>
  <a href="/">Home</a>
  <a href="/library">Library</a>
  <a href="/scan">Scan</a>
  <a href="/recommendations">Recommendations</a>
  <a href="/devices">Devices</a>
  <a href="/history">History</a>
</nav>
<h1>{html.escape(title)}</h1>
{body}
</body>
</html>"""


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> HTMLResponse:
    # Without this a failure is a bare "Internal Server Error" with the
    # traceback going nowhere a desktop-app user can see.
    logger.error("Unhandled error serving %s %s", request.method, request.url.path, exc_info=exc)
    where = f"<code>{html.escape(str(_log_dir / 'musictoolkit.log'))}</code>" if _log_dir else "the app's log file"
    body = f"""
    <p>Something went wrong while loading this page.</p>
    <pre>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</pre>
    <p>The full traceback was written to {where}.</p>
    """
    return HTMLResponse(_page("Something went wrong", body), status_code=500)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    conn = _connection()
    track_count = conn.execute("SELECT COUNT(*) AS c FROM tracks WHERE is_missing = 0").fetchone()["c"]
    pending_count = conn.execute("SELECT COUNT(*) AS c FROM recommendations WHERE status = 'new'").fetchone()["c"]
    device_count = conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"]
    history_count = conn.execute("SELECT COUNT(*) AS c FROM play_history").fetchone()["c"]
    conn.close()
    empty_hint = (
        '<p><strong>Your library is empty.</strong> <a href="/scan">Scan a music folder</a> to get started.</p>'
        if track_count == 0
        else ""
    )
    body = f"""
    {empty_hint}
    <ul>
      <li>{track_count} tracks in library</li>
      <li>{pending_count} recommendations awaiting review</li>
      <li>{device_count} known devices</li>
      <li>{history_count} play-history events</li>
    </ul>
    """
    return _page("Personal Music Toolkit", body)


@app.get("/library", response_class=HTMLResponse)
def library(q: str = "") -> str:
    conn = _connection()
    if q:
        like = f"%{q}%"
        rows = conn.execute(
            "SELECT * FROM tracks WHERE is_missing = 0 AND (artist LIKE ? OR title LIKE ? OR album LIKE ?) "
            "ORDER BY artist, album, track_number LIMIT 200",
            (like, like, like),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE is_missing = 0 ORDER BY artist, album, track_number LIMIT 200"
        ).fetchall()
    conn.close()

    search_box = f"""
    <form method="get">
      <input type="text" name="q" value="{html.escape(q)}" placeholder="Search artist/title/album">
      <button type="submit">Search</button>
    </form>
    """
    rows_html = "".join(
        f"<tr><td>{html.escape(r['artist'] or '')}</td><td>{html.escape(r['album'] or '')}</td>"
        f"<td>{html.escape(r['title'] or '')}</td><td>{r['track_number'] or ''}</td></tr>"
        for r in rows
    )
    body = f"""
    {search_box}
    <p>{len(rows)} tracks shown (max 200).</p>
    <table>
      <tr><th>Artist</th><th>Album</th><th>Title</th><th>#</th></tr>
      {rows_html}
    </table>
    """
    return _page("Library", body)


@app.get("/recommendations", response_class=HTMLResponse)
def recommendations() -> str:
    conn = _connection()
    pending = review_queue.list_pending(conn, limit=100)
    conn.close()

    rows_html = ""
    for r in pending:
        label = html.escape(r["artist_name"]) + (f" - {html.escape(r['track_name'])}" if r["track_name"] else "")
        rows_html += f"""
        <tr>
          <td>{label}</td>
          <td>{html.escape(r['source'])}</td>
          <td>{r['score']:.2f}</td>
          <td>{html.escape(r['reason'] or '')}</td>
          <td>
            <form method="post" action="/recommendations/{r['id']}/status">
              <button name="status" value="accepted">Accept</button>
              <button name="status" value="owned">Owned</button>
              <button name="status" value="dismissed">Dismiss</button>
            </form>
          </td>
        </tr>
        """
    body = f"""
    <p>{len(pending)} pending recommendations.</p>
    <table>
      <tr><th>Artist / Track</th><th>Source</th><th>Score</th><th>Reason</th><th>Action</th></tr>
      {rows_html}
    </table>
    """
    return _page("Recommendations", body)


@app.post("/recommendations/{recommendation_id}/status")
def update_recommendation_status(recommendation_id: int, status: str = Form(...)) -> RedirectResponse:
    conn = _connection()
    try:
        review_queue.set_status(conn, recommendation_id, status)
    finally:
        conn.close()
    return RedirectResponse(url="/recommendations", status_code=303)


@app.get("/devices", response_class=HTMLResponse)
def devices() -> str:
    conn = _connection()
    rows = conn.execute(
        """
        SELECT d.id, d.label, d.last_seen_mount_path, d.created_at, COUNT(m.id) AS synced_count
        FROM devices d
        LEFT JOIN sync_manifest m ON m.device_id = d.id
        GROUP BY d.id
        ORDER BY d.created_at DESC
        """
    ).fetchall()
    conn.close()

    rows_html = "".join(
        f"<tr><td>{html.escape(r['label'] or '')}</td><td>{html.escape(r['last_seen_mount_path'] or '')}</td>"
        f"<td>{r['synced_count']}</td></tr>"
        for r in rows
    )
    body = f"""
    <table>
      <tr><th>Label</th><th>Last-seen mount path</th><th>Files synced</th></tr>
      {rows_html}
    </table>
    """
    return _page("Devices", body)


@app.get("/history", response_class=HTMLResponse)
def history() -> str:
    conn = _connection()
    top_artists = conn.execute(
        """
        SELECT raw_artist_name, COUNT(*) AS play_count
        FROM play_history
        WHERE raw_artist_name IS NOT NULL
        GROUP BY raw_artist_name
        ORDER BY play_count DESC
        LIMIT 15
        """
    ).fetchall()
    conn.close()

    max_count = max((r["play_count"] for r in top_artists), default=1) or 1
    bars_html = "".join(
        f"""<div class="bar-row">
              <span>{html.escape(r['raw_artist_name'])} ({r['play_count']})</span>
              <div class="bar" style="width: {(r['play_count'] / max_count) * 100:.0f}%"></div>
            </div>"""
        for r in top_artists
    )
    body = f"""
    <p>Top artists by play count:</p>
    {bars_html or '<p>No play history imported yet — run <code>mtk import-spotify</code> first.</p>'}
    """
    return _page("Play History", body)


@dataclass
class _ScanState:
    running: bool = False
    root: str = ""
    done: int = 0
    total: int = 0
    result: scanner.ScanResult | None = None
    error: str | None = None


_scan_state = _ScanState()
_scan_lock = threading.Lock()
_scan_thread: threading.Thread | None = None


def _run_scan(root: Path, db_path: Path) -> None:
    conn = None
    try:
        # Own connection: sqlite3 connections can't be shared across threads.
        conn = connect(db_path)

        def on_progress(done: int, total: int) -> None:
            _scan_state.done = done
            _scan_state.total = total

        _scan_state.result = scanner.scan_library(conn, root, on_progress=on_progress)
    except Exception as exc:
        logger.exception("Library scan of %s failed", root)
        _scan_state.error = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()
        _scan_state.running = False


def _start_scan(root: Path) -> bool:
    """Returns False, starting nothing, if a scan is already running."""
    global _scan_thread
    if _db_path is None:
        raise RuntimeError("dashboard.configure(db_path) must be called before serving requests")
    with _scan_lock:
        if _scan_state.running:
            return False
        _scan_state.running = True
        _scan_state.root = str(root)
        _scan_state.done = 0
        _scan_state.total = 0
        _scan_state.result = None
        _scan_state.error = None
        _scan_thread = threading.Thread(target=_run_scan, args=(root, _db_path), daemon=True, name="library-scan")
        _scan_thread.start()
    return True


def _scan_view(form_error: str = "", last_path: str = "") -> str:
    state = _scan_state

    if state.running:
        if state.total:
            progress = f"{state.done} of {state.total} files ({state.done / state.total * 100:.0f}%)"
        else:
            progress = "looking for audio files..."
        body = f"""
        <p>Scanning <code>{html.escape(state.root)}</code>: {progress}</p>
        <p>This page refreshes by itself. The other pages keep working while the scan runs.</p>
        """
        return _page("Scan library", body, head_extra='<meta http-equiv="refresh" content="2">')

    notes = ""
    if state.error:
        notes = f"<p><strong>The last scan failed:</strong> {html.escape(state.error)}</p>"
    elif state.result is not None:
        r = state.result
        notes = (
            f"<p><strong>Scan finished</strong> for <code>{html.escape(state.root)}</code>: "
            f"{r.added} added, {r.updated} updated, {r.unchanged} unchanged, "
            f"{r.missing} marked missing, {r.errors} errors. "
            '<a href="/library">View the library</a>.</p>'
        )
    error_html = f'<p style="color: #b00020">{html.escape(form_error)}</p>' if form_error else ""
    prefill = last_path or state.root
    body = f"""
    {notes}
    {error_html}
    <p>Scanning reads the tags of the audio files in a folder (including subfolders) into the library
    database. It never changes your files.</p>
    <form method="post" action="/scan">
      <input type="text" id="path" name="path" size="60" value="{html.escape(prefill)}"
             placeholder="C:\\Users\\you\\Music" required>
      <button type="button" id="browse" hidden>Browse...</button>
      <button type="submit">Scan</button>
    </form>
    <script>
      // Only the desktop app provides a native folder picker; in a plain browser the typed path is used.
      if (window.mtk && window.mtk.selectFolder) {{
        const browse = document.getElementById("browse");
        browse.hidden = false;
        browse.addEventListener("click", async () => {{
          const picked = await window.mtk.selectFolder();
          if (picked) document.getElementById("path").value = picked;
        }});
      }}
    </script>
    """
    return _page("Scan library", body)


@app.get("/scan", response_class=HTMLResponse)
def scan_page() -> str:
    return _scan_view()


@app.post("/scan")
def start_scan(path: str = Form(...)) -> Response:
    # Explorer's "Copy as path" wraps the path in double quotes.
    root = Path(path.strip().strip('"')).expanduser()
    if not root.is_dir():
        return HTMLResponse(
            _scan_view(form_error=f"Folder not found: {path}", last_path=path), status_code=400
        )
    _start_scan(root)
    return RedirectResponse(url="/scan", status_code=303)
