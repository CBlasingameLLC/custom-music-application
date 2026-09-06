from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path | str, level: str = "INFO") -> logging.Logger:
    """Configure the shared 'musictoolkit' logger with rotating-file + console output.

    Every destructive action a module takes (tag write, file move, device
    prune) should log a clear before/after record through this logger — it's
    the tool's only audit trail, since there's no server watching over it.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("musictoolkit")
    logger.setLevel(level.upper())
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_dir / "musictoolkit.log", maxBytes=5_000_000, backupCount=3
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger
