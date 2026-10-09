from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from fastapi import Request

from musictoolkit.config import Config, save_config
from musictoolkit.web.jobs import JobManager


@dataclass
class AppContext:
    """Everything a request handler needs; one per running app (no globals)."""

    db_path: Path
    config_path: Path
    config: Config
    token: str | None = None  # None disables auth (tests); the desktop app always sets one
    jobs: JobManager = field(default_factory=JobManager)
    previews: dict[str, Any] = field(default_factory=dict)  # the last "show me first" result per tool, kept in memory

    @property
    def data_dir(self) -> Path:
        return self.config_path.parent

    @property
    def art_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "art"

    @property
    def log_dir(self) -> Path:
        return Path(self.config.logging.dir)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        # One short-lived connection per use: sqlite3 connections can't hop
        # between the worker threads FastAPI runs sync handlers on.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def save_config(self) -> None:
        save_config(self.config_path, self.config)


def same_path(a: str, b: str) -> bool:
    """Do these name the same place, ignoring letter case on Windows and a trailing slash?"""
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx
