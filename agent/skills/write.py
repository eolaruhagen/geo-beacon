"""Write/action agent skills for geo-beacon.

Every function mutates SQLite through the same connection helper FastAPI uses.
The model should never write raw SQL; these functions are its action surface.
"""
from __future__ import annotations

import json
import time
from typing import Any

from api.db import session
import api.db.broadcasts as db_broadcasts
import api.db.missions as db_missions
from api.db.hazards import bulk_insert_hazards
from api.db.hex_cells import rasterize_hazard_to_hex_flags
from agent.skills.read import _resolve_mission_id


ACTIVE_DISPATCH_STATUSES = ("pending", "acked", "in_progress")
VALID_SWEEP_TYPES = {"hasty", "efficient", "thorough"}
VALID_BROADCAST_KINDS = {"info", "warning", "recall", "finding_alert", "route_correction"}
VALID_HAZARD_KINDS = {"cliff", "water", "weather", "no_comms_zone", "wildlife", "other"}
VALID_HAZARD_SEVERITIES = {"info", "caution", "critical"}
VALID_MISSION_STATUSES = {"planning", "active", "subject_found", "suspended", "ended"}
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
# This is used before a recall or reassignment so the map does not show stale work.
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
# This keeps the history while making the newest order the one that matters.
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

    Use `reassign_searcher` if the searcher already has an active dispatch.
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
                    f"User {user_id} already has active dispatch {existing[0]['id']}; "
                    "use reassign_searcher or recall_searcher"
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


# Changes a searcher's current assignment to a new segment.
# It supersedes old orders so the phone and database agree on the latest plan.
def reassign_searcher(
    user_id: int,
    new_segment_id: int,
    sweep_type: str,
    instruction: str,
    reasoning: str,
    entry_lat: float | None = None,
    entry_lon: float | None = None,
    mission_id: int | None = None,
) -> dict[str, Any]:
    """Supersede a searcher's active dispatch and issue a new segment assignment."""
    _require_reason(reasoning)
    if sweep_type not in VALID_SWEEP_TYPES:
        raise ValueError(f"Invalid sweep_type {sweep_type!r}")
    mid = _resolve_mission_id(mission_id)
    with session() as conn:
        conn.execute("BEGIN")
        try:
            user = _require_user(conn, mid, user_id)
            segment = _require_segment(conn, mid, new_segment_id)
            existing = _active_dispatches(conn, mid, user_id)
            _clear_segment_assignments(conn, mid, user_id)
            if entry_lat is None or entry_lon is None:
                entry_lat = float(segment["center_lat"])
                entry_lon = float(segment["center_lon"])
            dispatch_id = _insert_dispatch(
                conn, mid, user_id, new_segment_id, sweep_type,
                entry_lat, entry_lon, instruction, reasoning,
            )
            superseded_count = _supersede_dispatches(conn, existing, dispatch_id)
            conn.execute("UPDATE users SET status = 'dispatched' WHERE id = ?", (user_id,))
            conn.execute(
                """
                UPDATE segments
                SET status = 'assigned',
                    assigned_user_id = ?,
                    sweep_type = ?,
                    target_pod = ?
                WHERE id = ?
                """,
                (user_id, sweep_type, TARGET_POD[sweep_type], new_segment_id),
            )
            conn.execute(
                """
                INSERT INTO broadcasts (mission_id, scope, kind, message, ts)
                VALUES (?, ?, 'route_correction', ?, ?)
                """,
                (
                    mid,
                    db_broadcasts.user_scope(user_id),
                    f"Assignment updated: {segment['name']}. {instruction}",
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
        "superseded_count": superseded_count,
        "user_id": user_id,
        "callsign": user["callsign"],
        "segment_id": new_segment_id,
        "segment_name": segment["name"],
        "status": "pending",
    }


# Calls a searcher back instead of sending them to a segment.
# This creates a recall dispatch and targeted phone message.
def recall_searcher(
    user_id: int,
    instruction: str,
    reasoning: str,
    return_lat: float | None = None,
    return_lon: float | None = None,
    mission_id: int | None = None,
) -> dict[str, Any]:
    """Recall a searcher to staging or another non-segment destination."""
    _require_reason(reasoning)
    mid = _resolve_mission_id(mission_id)
    mission = db_missions.get_mission(mid)
    if mission is None:
        raise ValueError(f"Mission {mid} not found")
    if return_lat is None:
        return_lat = float(mission["pls_lat"])
    if return_lon is None:
        return_lon = float(mission["pls_lon"])
    with session() as conn:
        conn.execute("BEGIN")
        try:
            user = _require_user(conn, mid, user_id)
            existing = _active_dispatches(conn, mid, user_id)
            _clear_segment_assignments(conn, mid, user_id)
            dispatch_id = _insert_dispatch(
                conn, mid, user_id, None, None, return_lat, return_lon,
                instruction, reasoning,
            )
            superseded_count = _supersede_dispatches(conn, existing, dispatch_id)
            conn.execute("UPDATE users SET status = 'returning' WHERE id = ?", (user_id,))
            conn.execute(
                """
                INSERT INTO broadcasts (mission_id, scope, kind, message, ts)
                VALUES (?, ?, 'recall', ?, ?)
                """,
                (mid, db_broadcasts.user_scope(user_id), instruction, int(time.time())),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {
        "mission_id": mid,
        "dispatch_id": dispatch_id,
        "superseded_count": superseded_count,
        "user_id": user_id,
        "callsign": user["callsign"],
        "status": "pending",
    }


# Sends a message to everyone or one specific user.
# This is how the agent talks to phones without creating a dispatch.
def broadcast(
    scope: str,
    kind: str,
    message: str,
    reasoning: str,
    mission_id: int | None = None,
) -> dict[str, Any]:
    """Send a mission-wide or user-targeted broadcast.

    Scope must be `all` or `user:{id}`.
    """
    _require_reason(reasoning)
    if kind not in VALID_BROADCAST_KINDS:
        raise ValueError(f"Invalid broadcast kind {kind!r}")
    mid = _resolve_mission_id(mission_id)
    if scope != "all" and not scope.startswith("user:"):
        raise ValueError("scope must be 'all' or 'user:{id}'")
    if scope.startswith("user:"):
        try:
            target_user_id = int(scope.split(":", 1)[1])
        except ValueError as exc:
            raise ValueError("user scope must be formatted as user:{integer_id}") from exc
        with session() as conn:
            _require_user(conn, mid, target_user_id)
    broadcast_id = db_broadcasts.insert_broadcast(mid, scope, kind, message)
    return {
        "mission_id": mid,
        "broadcast_id": broadcast_id,
        "scope": scope,
        "kind": kind,
        "message": message,
    }


# Adds a hazard polygon to the mission.
# It also colors affected hexes as dangerous and warns assigned searchers nearby.
def flag_hazard(
    geom_geojson: dict[str, Any],
    kind: str,
    severity: str,
    description: str,
    reasoning: str,
    mission_id: int | None = None,
) -> dict[str, Any]:
    """Insert a hazard polygon, rasterize danger flags, and warn affected searchers."""
    _require_reason(reasoning)
    if geom_geojson.get("type") != "Polygon":
        raise ValueError("geom_geojson must be a GeoJSON Polygon")
    if kind not in VALID_HAZARD_KINDS:
        raise ValueError(f"Invalid hazard kind {kind!r}")
    if severity not in VALID_HAZARD_SEVERITIES:
        raise ValueError(f"Invalid hazard severity {severity!r}")
    mid = _resolve_mission_id(mission_id)
    hazard_ids = bulk_insert_hazards(
        mid,
        [{
            "kind": kind,
            "severity": severity,
            "description": description,
            "poly_geojson": geom_geojson,
        }],
    )
    hazard_id = hazard_ids[0]
    hexes_flagged = rasterize_hazard_to_hex_flags(mid, hazard_id)

    with session() as conn:
        affected = conn.execute(
            """
            SELECT DISTINCT u.id, u.callsign
            FROM users u
            JOIN dispatches d ON d.user_id = u.id
            JOIN segments s ON s.id = d.segment_id
            JOIN hazards h ON h.id = ?
            WHERE d.mission_id = ?
              AND d.status IN ('pending', 'acked', 'in_progress')
              AND ST_Intersects(s.geom, h.geom)
            """,
            (hazard_id, mid),
        ).fetchall()
    affected_users = [dict(row) for row in affected]
    for user in affected_users:
        db_broadcasts.insert_broadcast(
            mid,
            db_broadcasts.user_scope(user["id"]),
            "warning",
            f"Hazard near your assignment: {description}",
        )
    return {
        "mission_id": mid,
        "hazard_id": hazard_id,
        "hexes_flagged": hexes_flagged,
        "affected_users": affected_users,
    }


# Changes one segment's probability number.
# It rescales the other segments so the mission probabilities still add up.
def update_segment_poa(
    segment_id: int,
    new_poa: float,
    reasoning: str,
    mission_id: int | None = None,
) -> dict[str, Any]:
    """Set one segment's POA and renormalize the rest of the mission to sum to 1."""
    _require_reason(reasoning)
    if not 0 <= new_poa <= 1:
        raise ValueError("new_poa must be between 0 and 1")
    mid = _resolve_mission_id(mission_id)
    with session() as conn:
        conn.execute("BEGIN")
        try:
            _require_segment(conn, mid, segment_id)
            row = conn.execute(
                """
                SELECT COALESCE(SUM(poa), 0) AS other_total
                FROM segments
                WHERE mission_id = ? AND id != ?
                """,
                (mid, segment_id),
            ).fetchone()
            other_total = float(row["other_total"] or 0.0)
            if other_total > 0:
                scale = (1.0 - new_poa) / other_total
                conn.execute(
                    """
                    UPDATE segments
                    SET poa = poa * ?, pos = (poa * ?) * pod
                    WHERE mission_id = ? AND id != ?
                    """,
                    (scale, scale, mid, segment_id),
                )
            conn.execute(
                """
                UPDATE segments
                SET poa = ?, pos = ? * pod
                WHERE id = ? AND mission_id = ?
                """,
                (new_poa, new_poa, segment_id, mid),
            )
            total_row = conn.execute(
                "SELECT SUM(poa) AS total_poa FROM segments WHERE mission_id = ?",
                (mid,),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {
        "mission_id": mid,
        "segment_id": segment_id,
        "new_poa": new_poa,
        "total_poa": float(total_row["total_poa"] or 0.0),
    }


# Changes the mission's overall status.
# This is used for big events like subject found, suspended, or ended.
def update_mission_status(
    new_status: str,
    reasoning: str,
    mission_id: int | None = None,
) -> dict[str, Any]:
    """Update mission lifecycle status, for example subject_found or suspended."""
    _require_reason(reasoning)
    if new_status not in VALID_MISSION_STATUSES:
        raise ValueError(f"Invalid mission status {new_status!r}")
    mid = _resolve_mission_id(mission_id)
    db_missions.set_status(mid, new_status)
    if new_status in {"subject_found", "suspended", "ended"}:
        db_broadcasts.insert_broadcast(
            mid,
            "all",
            "info" if new_status == "subject_found" else "warning",
            f"Mission status updated to {new_status}.",
        )
    return {"mission_id": mid, "status": new_status}
