from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.recommend import review_queue


def _insert_recommendation(conn, artist_name: str, score: float, status: str = "new") -> int:
    conn.execute(
        "INSERT INTO recommendations (artist_name, track_name, source, score, status) VALUES (?, 't', 's', ?, ?)",
        (artist_name, score, status),
    )
    conn.commit()
    return conn.execute("SELECT id FROM recommendations WHERE artist_name = ?", (artist_name,)).fetchone()["id"]


def test_list_pending_orders_by_score_descending(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    _insert_recommendation(conn, "Low Score", 0.1)
    _insert_recommendation(conn, "High Score", 0.9)
    _insert_recommendation(conn, "Already Reviewed", 1.0, status="accepted")

    pending = review_queue.list_pending(conn)

    assert [row["artist_name"] for row in pending] == ["High Score", "Low Score"]
    conn.close()


def test_set_status_updates_row_and_sets_reviewed_date(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    rec_id = _insert_recommendation(conn, "Some Artist", 0.5)

    review_queue.set_status(conn, rec_id, "accepted")

    row = conn.execute("SELECT * FROM recommendations WHERE id = ?", (rec_id,)).fetchone()
    assert row["status"] == "accepted"
    assert row["date_reviewed"] is not None
    conn.close()


def test_set_status_rejects_invalid_status(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    rec_id = _insert_recommendation(conn, "Some Artist", 0.5)
    with pytest.raises(ValueError):
        review_queue.set_status(conn, rec_id, "bogus")
    conn.close()
