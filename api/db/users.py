from __future__ import annotations

import secrets
import time

from api.db import session


def create_user(
    display_name: str,
    callsign: str | None,
    role: str,
) -> dict:
    """Inserts user with status='standby', random hex bearer_token (32 bytes).
    Returns {id, display_name, callsign, role, status, bearer_token, created_ts}."""
    token = secrets.token_hex(32)
    now = int(time.time())
    with session() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (display_name, callsign, role, status, bearer_token, created_ts)
            VALUES (?, ?, ?, 'standby', ?, ?)
            """,
            (display_name, callsign, role, token, now),
        )
        user_id = cur.lastrowid
        return {
            "id": user_id,
            "display_name": display_name,
            "callsign": callsign,
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
