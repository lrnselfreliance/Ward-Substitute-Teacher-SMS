"""Engine and session setup.

SQLite in WAL mode. Single writer by design -- see the one-worker note in the
plan; two uvicorn workers would mean two schedulers racing to send the same
batch twice.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base


def _apply_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def make_engine(db_path: str) -> Engine:
    if db_path == ":memory:":
        # StaticPool keeps every session on the same connection, otherwise each
        # one would get its own empty in-memory database.
        engine = create_engine(
            "sqlite://",
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{os.path.abspath(db_path)}", future=True)

    event.listen(engine, "connect", _apply_pragmas)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


# Columns added after the first release. create_all() only creates whole
# tables, so a live database needs these bolted on. Order matters only if one
# depends on another.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("request", "requested_by_id", "INTEGER REFERENCES person(id)"),
    ("person", "banned", "BOOLEAN NOT NULL DEFAULT 0"),
    ("person", "banned_at", "DATETIME"),
    ("person", "banned_by_id", "INTEGER REFERENCES person(id)"),
    ("person", "ban_reason", "TEXT"),
)


def _add_missing_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            present = {c["name"] for c in inspector.get_columns(table)}
            if column not in present:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
