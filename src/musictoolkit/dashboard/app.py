from __future__ import annotations

import html
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from musictoolkit.recommend import review_queue

app = FastAPI(title="Personal Music Toolkit Dashboard")

_db_path: Path | None = None


def configure(db_path: Path) -> None:
    """Must be called once before serving requests — set by `mtk dashboard`
    to whichever DB the rest of the CLI is already using."""
    global _db_path
    _db_path = db_path


def _connection() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("dashboard.configure(db_path) must be called before serving requests")
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
<title>{html.escape(title)}</title>
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
  <a href="/recommendations">Recommendations</a>
  <a href="/devices">Devices</a>
  <a href="/history">History</a>
</nav>
<h1>{html.escape(title)}</h1>
{body}
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    conn = _connection()
    track_count = conn.execute("SELECT COUNT(*) AS c FROM tracks WHERE is_missing = 0").fetchone()["c"]
    pending_count = conn.execute("SELECT COUNT(*) AS c FROM recommendations WHERE status = 'new'").fetchone()["c"]
    device_count = conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"]
    history_count = conn.execute("SELECT COUNT(*) AS c FROM play_history").fetchone()["c"]
    conn.close()
    body = f"""
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
