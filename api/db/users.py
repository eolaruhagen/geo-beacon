from __future__ import annotations

import secrets
import time

from api.db import session


def create_user(
    display_name: str,
    callsign: str | None,
    role: str = "searcher",
    current_mission_id: int | None = None,
) -> dict:
    """Inserts user with status='standby', random hex bearer_token (32 bytes).
    Returns {id, display_name, callsign, role, status, bearer_token,
    current_mission_id, created_ts}.

    Callsign uniqueness is scoped per-mission via UNIQUE(current_mission_id,
    callsign) in migrations/001_init.sql. On collision within a mission,
    SQLite raises `sqlite3.IntegrityError`; the route layer translates that to
    HTTP 409.
    """
    token = secrets.token_hex(32)
    now = int(time.time())
    with session() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (display_name, callsign, role, status, bearer_token,
                               current_mission_id, created_ts)
            VALUES (?, ?, ?, 'standby', ?, ?, ?)
            """,
            (display_name, callsign, role, token, current_mission_id, now),
        )
        user_id = cur.lastrowid
        return {
            "id": user_id,
            "display_name": display_name,
            "callsign": callsign,
            "role": role,
            "status": "standby",
            "bearer_token": token,
            "current_mission_id": current_mission_id,
            "created_ts": now,
        }


def set_current_mission(user_id: int, mission_id: int) -> None:
    """Set the user's current_mission_id. Used when a user creates or joins a
    mission, so subsequent lookups (e.g. active_mission_id_for_user) and the
    per-mission callsign uniqueness constraint both work directly."""
    with session() as conn:
        conn.execute(
            "UPDATE users SET current_mission_id = ? WHERE id = ?",
            (mission_id, user_id),
        )


def set_user_status(user_id: int, status: str) -> None:
    """Update users.status. CHECK constraint in 001_init.sql enforces the
    allowed values ('standby','dispatched','on_segment','returning',
    'no_comms','off_duty') — sqlite3 raises IntegrityError on a bad value."""
    with session() as conn:
        conn.execute(
            "UPDATE users SET status = ? WHERE id = ?",
            (status, user_id),
        )


def get_user_by_token(token: str) -> dict | None:
    """Bearer-token lookup. Returns full user row or None."""
    with session() as conn:
        row = conn.execute(
            "SELECT id, display_name, callsign, phone, role, status, bearer_token, "
            "current_mission_id, created_ts "
            "FROM users WHERE bearer_token = ?",
            (token,),
        ).fetchone()
        return dict(row) if row else None


def get_user(user_id: int) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT id, display_name, callsign, phone, role, status, bearer_token, "
            "current_mission_id, created_ts "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
