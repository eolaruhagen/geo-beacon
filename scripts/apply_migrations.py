#!/usr/bin/env python3
"""Apply pending SQL migrations idempotently. Run at every worker / API startup.

Looks for migrations/*.sql, lexically ordered, applies those not present in
schema_migrations table. Each migration runs in a single transaction.

Env vars:
  MISSION_DB_PATH      defaults to /home/asus/sqlite/mission.db
  MIGRATIONS_DIR       defaults to <repo>/migrations
  SPATIALITE_PATH      override the SpatiaLite extension search; otherwise tries
                       common Ubuntu/macOS install locations
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB_PATH = "/home/asus/sqlite/mission.db"
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SPATIALITE_CANDIDATES = [
    "mod_spatialite",
    "mod_spatialite.so",
    "/usr/lib/aarch64-linux-gnu/mod_spatialite",
    "/usr/lib/x86_64-linux-gnu/mod_spatialite",
    "/opt/homebrew/lib/mod_spatialite",
    "/usr/local/lib/mod_spatialite",
]


def load_spatialite(conn: sqlite3.Connection) -> str:
    """Load mod_spatialite into the connection. Returns the path that worked."""
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
            return path
        except sqlite3.OperationalError as e:
            last_err = e
    conn.enable_load_extension(False)
    raise RuntimeError(
        f"Could not load mod_spatialite from any of {candidates}. "
        f"Install with `sudo apt install libsqlite3-mod-spatialite` "
        f"(Ubuntu) or `brew install libspatialite` (macOS). "
        f"Last error: {last_err}"
    )


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name        TEXT    PRIMARY KEY,
            applied_ts  INTEGER NOT NULL
        )
        """
    )
    conn.commit()


def apply(db_path: str, migrations_dir: Path) -> int:
    """Apply pending migrations. Returns the number of migrations applied."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        spatialite_path = load_spatialite(conn)
        print(f"[migrations] loaded spatialite from {spatialite_path}", flush=True)
        ensure_migrations_table(conn)

        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        files = sorted(p for p in migrations_dir.iterdir() if p.suffix == ".sql")
        if not files:
            print(f"[migrations] no .sql files in {migrations_dir}", flush=True)
            return 0

        applied_count = 0
        for f in files:
            if f.name in applied:
                continue
            print(f"[migrations] applying {f.name} ...", flush=True)
            sql = f.read_text()
            try:
                # executescript implicitly commits any open tx; we wrap our own.
                conn.execute("BEGIN")
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (name, applied_ts) VALUES (?, ?)",
                    (f.name, int(time.time())),
                )
                conn.commit()
                applied_count += 1
            except Exception as e:
                conn.rollback()
                print(f"[migrations] FAILED {f.name}: {e}", file=sys.stderr, flush=True)
                raise

        if applied_count == 0:
            print("[migrations] up to date", flush=True)
        else:
            print(f"[migrations] applied {applied_count} migration(s)", flush=True)
        return applied_count
    finally:
        conn.close()


def main() -> int:
    db_path = os.environ.get("MISSION_DB_PATH", DEFAULT_DB_PATH)
    migrations_dir = Path(os.environ.get("MIGRATIONS_DIR", str(DEFAULT_MIGRATIONS_DIR)))
    print(f"[migrations] db={db_path} dir={migrations_dir}", flush=True)
    apply(db_path, migrations_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
