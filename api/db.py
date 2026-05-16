"""SQLite + SpatiaLite connection helper used by FastAPI handlers and workers.

Single point of truth for: DB path, SpatiaLite loading, pragmas, row_factory.
Always use `connect()` from here; never call sqlite3.connect directly.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterator
from contextlib import contextmanager

DEFAULT_DB_PATH = "/home/asus/sqlite/mission.db"
SPATIALITE_CANDIDATES = [
    "mod_spatialite",
    "mod_spatialite.so",
    "/usr/lib/aarch64-linux-gnu/mod_spatialite",
    "/usr/lib/x86_64-linux-gnu/mod_spatialite",
    "/opt/homebrew/lib/mod_spatialite",
    "/usr/local/lib/mod_spatialite",
]


def db_path() -> str:
    return os.environ.get("MISSION_DB_PATH", DEFAULT_DB_PATH)


def _load_spatialite(conn: sqlite3.Connection) -> None:
    override = os.environ.get("SPATIALITE_PATH")
    candidates = [override] if override else SPATIALITE_CANDIDATES
    conn.enable_load_extension(True)
    last_err: Exception | None = None
    for path in candidates:
        if not path:
            continue
        try:
            conn.load_extension(path)
            conn.enable_load_extension(False)
            return
        except sqlite3.OperationalError as e:
            last_err = e
    conn.enable_load_extension(False)
    raise RuntimeError(f"Could not load mod_spatialite: {last_err}")


def connect() -> sqlite3.Connection:
    """Open a SpatiaLite-loaded SQLite connection with our standard pragmas.

    Caller owns the connection lifecycle. For request-scoped use in FastAPI
    handlers, prefer `with session() as conn:` below.
    """
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    _load_spatialite(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Context-manager wrapper that guarantees close()."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
