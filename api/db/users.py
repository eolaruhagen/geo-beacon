from __future__ import annotations

import secrets
import sqlite3
import time

from api.db import session


def _disambiguate_callsign(conn: sqlite3.Connection, callsign: str | None) -> str | None:
    """If `callsign` is already taken in users.callsign (which is UNIQUE),
    append `-2`, `-3`, ... until we find a free one. Mostly a dev-loop nicety —
    real callsign scoping should be per-mission but that needs a schema change."""
    if callsign is None:
        return None
    row = conn.execute("SELECT 1 FROM users WHERE callsign = ?", (callsign,)).fetchone()
    if row is None:
        return callsign
    n = 2
    while True:
        candidate = f"{callsign}-{n}"
        row = conn.execute("SELECT 1 FROM users WHERE callsign = ?", (candidate,)).fetchone()
        if row is None:
            return candidate
        n += 1


def create_user(
    display_name: str,
    callsign: str | None,
    role: str = "searcher",
) -> dict:
    """Inserts user with status='standby', random hex bearer_token (32 bytes).
    Returns {id, display_name, callsign, role, status, bearer_token, created_ts}.

    If `callsign` is already taken, auto-disambiguates to `<callsign>-2`,
    `<callsign>-3`, etc. so repeat smoke-tests and same-name joins both work."""
    token = secrets.token_hex(32)
    now = int(time.time())
    with session() as conn:
        final_callsign = _disambiguate_callsign(conn, callsign)
        cur = conn.execute(
            """
            INSERT INTO users (display_name, callsign, role, status, bearer_token, created_ts)
            VALUES (?, ?, ?, 'standby', ?, ?)
            """,
            (display_name, final_callsign, role, token, now),
        )
        user_id = cur.lastrowid
        return {
            "id": user_id,
            "display_name": display_name,
            "callsign": final_callsign,
            "role": role,
            "status": "standby",
            "bearer_token": token,
            "created_ts": now,
        }


def get_user_by_token(token: str) -> dict | None:
    """Bearer-token lookup. Returns full user row or None."""
    with session() as conn:
        row = conn.execute(
            "SELECT id, display_name, callsign, phone, role, status, bearer_token, created_ts "
            "FROM users WHERE bearer_token = ?",
            (token,),
        ).fetchone()
        return dict(row) if row else None


def get_user(user_id: int) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT id, display_name, callsign, phone, role, status, bearer_token, created_ts "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
