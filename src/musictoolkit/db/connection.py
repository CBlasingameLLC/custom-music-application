from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

MIGRATIONS_PACKAGE = "musictoolkit.db.migrations"


def _migration_files() -> list[tuple[int, str, str]]:
    """Return (version, name, sql_text) for every migration, sorted by version."""
    migrations_dir = resources.files(MIGRATIONS_PACKAGE)
    files = []
    for entry in migrations_dir.iterdir():
        if entry.name.endswith(".sql"):
            version = int(entry.name.split("_", 1)[0])
            files.append((version, entry.name, entry.read_text(encoding="utf-8")))
    return sorted(files, key=lambda item: item[0])


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite DB and bring it up to the latest schema."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, _name, sql in _migration_files():
        if version > current_version:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
