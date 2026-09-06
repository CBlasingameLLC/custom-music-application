from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

_VALID_STATUSES = ("owned", "dismissed", "accepted")


def list_pending(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM recommendations WHERE status = 'new' ORDER BY score DESC LIMIT ?", (limit,)
    ).fetchall()


def set_status(conn: sqlite3.Connection, recommendation_id: int, status: str) -> None:
    """'accepted' is where this tool's responsibility ends — acquiring the
    actual file is manual and entirely the user's, by design."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"Invalid status: {status!r}, must be one of {_VALID_STATUSES}")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE recommendations SET status = ?, date_reviewed = ? WHERE id = ?", (status, now, recommendation_id)
    )
    conn.commit()
