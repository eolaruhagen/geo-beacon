"""Write/action agent skills for geo-beacon.

Surviving subset after the commander-grade tools were deprecated alongside
the §10 Mission Brief loop (see docs/2026-05-16-dispatch-agent.md and
docs/routing-agent-implementation.md). Routing agent will land a
`dispatch_to_cell` sibling here; until then, `dispatch_searcher` is the
only public write skill.
"""
from __future__ import annotations

import time
from typing import Any

from api.db import session
import api.db.broadcasts as db_broadcasts
from agent.skills.read import _resolve_mission_id


ACTIVE_DISPATCH_STATUSES = ("pending", "acked", "in_progress")
VALID_SWEEP_TYPES = {"hasty", "efficient", "thorough"}
TARGET_POD = {"hasty": 0.50, "efficient": 0.70, "thorough": 0.85}


# Makes sure every database-changing action explains why it happened.
# This keeps the agent from silently changing the mission.
def _require_reason(reasoning: str) -> None:
    if not reasoning or not reasoning.strip():
        raise ValueError("reasoning is required for every write skill")


# Checks that a user exists and belongs to this mission.
# It also makes sure the user is a searcher before assigning work.
def _require_user(conn, mission_id: int, user_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, display_name, callsign, role, status, current_mission_id
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"User {user_id} not found")
    user = dict(row)
    if user["current_mission_id"] != mission_id:
        raise ValueError(f"User {user_id} is not in mission {mission_id}")
    if user["role"] != "searcher":
        raise ValueError(f"User {user_id} is role={user['role']!r}; only searchers can be dispatched")
    return user


# Checks that a segment exists in this mission.
# It returns the segment's basic info and center point.
def _require_segment(conn, mission_id: int, segment_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, name, status, assigned_user_id, X(Centroid(geom)) AS center_lon,
               Y(Centroid(geom)) AS center_lat
        FROM segments
        WHERE id = ? AND mission_id = ?
        """,
        (segment_id, mission_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"Segment {segment_id} not found in mission {mission_id}")
    return dict(row)


# Finds unfinished orders for one searcher.
# This prevents the agent from accidentally giving two active jobs at once.
def _active_dispatches(conn, mission_id: int, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, segment_id, status
        FROM dispatches
        WHERE mission_id = ? AND user_id = ?
          AND status IN ('pending', 'acked', 'in_progress')
        ORDER BY issued_ts DESC
        """,
        (mission_id, user_id),
    ).fetchall()
    return [dict(row) for row in rows]


# Removes old segment ownership for a searcher.
# Used by dispatch flows to keep the segment map consistent with the dispatch table.
def _clear_segment_assignments(conn, mission_id: int, user_id: int) -> None:
    conn.execute(
        """
        UPDATE segments
        SET status = 'unassigned',
            assigned_user_id = NULL,
            sweep_type = NULL,
            target_pod = NULL
        WHERE mission_id = ?
          AND assigned_user_id = ?
          AND status IN ('assigned', 'in_progress')
        """,
        (mission_id, user_id),
    )


# Creates the actual dispatch row in the database.
# The phone sees this later through FastAPI polling.
def _insert_dispatch(
    conn,
    mission_id: int,
    user_id: int,
    segment_id: int | None,
    sweep_type: str | None,
    entry_lat: float | None,
    entry_lon: float | None,
    instruction: str,
    reasoning: str,
) -> int:
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO dispatches (
            mission_id, user_id, segment_id, sweep_type, entry_lat, entry_lon,
            instruction, reasoning, status, issued_ts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            mission_id,
            user_id,
            segment_id,
            sweep_type,
            entry_lat,
            entry_lon,
            instruction,
            reasoning,
            now,
        ),
    )
    return int(cur.lastrowid)


# Marks older active dispatches as replaced by a newer one.
# Used by the routing-agent dispatch-to-cell flow to supersede the previous target.
def _supersede_dispatches(conn, dispatches: list[dict[str, Any]], superseded_by: int) -> int:
    count = 0
    for dispatch in dispatches:
        conn.execute(
            """
            UPDATE dispatches
            SET status = 'superseded', superseded_by = ?
            WHERE id = ?
            """,
            (superseded_by, dispatch["id"]),
        )
        count += 1
    return count


# Sends an idle searcher to search a segment.
# It updates the user, segment, dispatch table, and a phone broadcast together.
def dispatch_searcher(
    user_id: int,
    segment_id: int,
    sweep_type: str,
    instruction: str,
    reasoning: str,
    entry_lat: float | None = None,
    entry_lon: float | None = None,
    mission_id: int | None = None,
) -> dict[str, Any]:
    """Assign an idle searcher to a segment and notify their phone.

    Fails if the searcher already has an active dispatch. The routing agent's
    forthcoming `dispatch_to_cell` will handle supersession via
    `_supersede_dispatches` directly.
    """
    _require_reason(reasoning)
    if sweep_type not in VALID_SWEEP_TYPES:
        raise ValueError(f"Invalid sweep_type {sweep_type!r}")
    mid = _resolve_mission_id(mission_id)
    with session() as conn:
        conn.execute("BEGIN")
        try:
            user = _require_user(conn, mid, user_id)
            segment = _require_segment(conn, mid, segment_id)
            existing = _active_dispatches(conn, mid, user_id)
            if existing:
                raise ValueError(
                    f"User {user_id} already has active dispatch {existing[0]['id']}"
                )
            if entry_lat is None or entry_lon is None:
                entry_lat = float(segment["center_lat"])
                entry_lon = float(segment["center_lon"])
            dispatch_id = _insert_dispatch(
                conn, mid, user_id, segment_id, sweep_type,
                entry_lat, entry_lon, instruction, reasoning,
            )
            conn.execute(
                """
                UPDATE users SET status = 'dispatched' WHERE id = ?
                """,
                (user_id,),
            )
            conn.execute(
                """
                UPDATE segments
                SET status = 'assigned',
                    assigned_user_id = ?,
                    sweep_type = ?,
                    target_pod = ?
                WHERE id = ?
                """,
                (user_id, sweep_type, TARGET_POD[sweep_type], segment_id),
            )
            conn.execute(
                """
                INSERT INTO broadcasts (mission_id, scope, kind, message, ts)
                VALUES (?, ?, 'info', ?, ?)
                """,
                (
                    mid,
                    db_broadcasts.user_scope(user_id),
                    f"New assignment: {segment['name']}. {instruction}",
                    int(time.time()),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {
        "mission_id": mid,
        "dispatch_id": dispatch_id,
        "user_id": user_id,
        "callsign": user["callsign"],
        "segment_id": segment_id,
        "segment_name": segment["name"],
        "status": "pending",
    }
