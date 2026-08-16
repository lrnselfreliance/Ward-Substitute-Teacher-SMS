"""Nightly SQLite backup.

VACUUM INTO rather than cp: it is safe against a live database, whereas
copying a file mid-write can produce a corrupt snapshot that only reveals
itself when you actually need it.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

KEEP_DAYS = 30


def backup(config: Config) -> Path | None:
    if config.db_path == ":memory:":
        return None

    out_dir = Path(config.backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    target = out_dir / f"substitute-{stamp}.db"

    try:
        if target.exists():
            target.unlink()
        with sqlite3.connect(config.db_path) as conn:
            conn.execute("VACUUM INTO ?", (str(target),))
        _prune(out_dir)
        log.info("backup written to %s", target)
        return target
    except Exception:
        log.exception("backup failed")
        return None


def _prune(out_dir: Path) -> None:
    backups = sorted(out_dir.glob("substitute-*.db"))
    for stale in backups[:-KEEP_DAYS]:
        stale.unlink(missing_ok=True)
