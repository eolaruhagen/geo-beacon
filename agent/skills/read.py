"""Read-only agent skills for geo-beacon.

These functions are the agent's safe view of mission state. They are the
surviving subset after the §10 Mission Brief loop was deprecated in favor of
the per-volunteer routing agent (see docs/2026-05-16-routing-agent.md).
"""
from __future__ import annotations

import math
import time
from typing import Any

from api.db import session
import api.db.missions as db_missions


ACTIVE_DISPATCH_STATUSES = ("pending", "acked", "in_progress")


# Measures the distance between two GPS dots.
# Used by surviving callers (sim/debug) and by the routing pre-compute.
def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# Picks which mission we are talking about.
# If nobody gives a mission id, it grabs the newest active mission.
def _resolve_mission_id(mission_id: int | None = None) -> int:
    """Return the requested mission id, or the newest active mission id."""
    if mission_id is not None:
        mission = db_missions.get_mission(mission_id)
        if mission is None:
            raise ValueError(f"Mission {mission_id} not found")
        return mission_id

    with session() as conn:
        row = conn.execute(
            """
            SELECT id FROM missions
            WHERE status = 'active'
            ORDER BY started_ts DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise ValueError("No active mission found")
    return int(row["id"])


# Lists the missions that are currently running.
# Used by the routing-agent worker to know which missions to loop over.
def active_missions() -> list[dict[str, Any]]:
    """Return active missions, newest first."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT id, name, status, subject_description, pls_lat, pls_lon,
                   pls_ts, started_ts, created_by_user_id
            FROM missions
            WHERE status = 'active'
            ORDER BY started_ts DESC, id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


# Shows who is in the mission and what each person is doing.
# It also attaches each person's latest GPS ping and current assignment.
def list_searchers(mission_id: int | None = None) -> list[dict[str, Any]]:
    """List searchers/observers in a mission with latest ping and active dispatch."""
    mid = _resolve_mission_id(mission_id)
    with session() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.display_name, u.callsign, u.role, u.status,
                   p.ts AS ping_ts, p.lat AS ping_lat, p.lon AS ping_lon,
                   p.accuracy_m AS ping_accuracy_m, p.battery_pct AS ping_battery_pct,
                   d.id AS dispatch_id, d.segment_id, d.sweep_type,
                   d.status AS dispatch_status, d.issued_ts, d.instruction,
                   s.name AS segment_name, s.pod AS segment_pod,
                   s.target_pod AS segment_target_pod
            FROM users u
            LEFT JOIN pings p ON p.id = (
              SELECT id FROM pings
              WHERE user_id = u.id AND mission_id = ?
              ORDER BY ts DESC
              LIMIT 1
            )
            LEFT JOIN dispatches d ON d.id = (
              SELECT id FROM dispatches
              WHERE user_id = u.id AND mission_id = ?
                AND status IN ('pending', 'acked', 'in_progress')
              ORDER BY issued_ts DESC
              LIMIT 1
            )
            LEFT JOIN segments s ON s.id = d.segment_id
            WHERE u.current_mission_id = ?
              AND u.role IN ('searcher', 'observer')
            ORDER BY COALESCE(u.callsign, u.display_name), u.id
            """,
            (mid, mid, mid),
        ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        latest_ping = None
        if d["ping_ts"] is not None:
            latest_ping = {
                "ts": d.pop("ping_ts"),
                "lat": d.pop("ping_lat"),
                "lon": d.pop("ping_lon"),
                "accuracy_m": d.pop("ping_accuracy_m"),
                "battery_pct": d.pop("ping_battery_pct"),
            }
        else:
            for key in ("ping_ts", "ping_lat", "ping_lon", "ping_accuracy_m", "ping_battery_pct"):
                d.pop(key, None)

        active_dispatch = None
        if d["dispatch_id"] is not None:
            active_dispatch = {
                "id": d.pop("dispatch_id"),
                "segment_id": d.pop("segment_id"),
                "segment_name": d.pop("segment_name"),
                "sweep_type": d.pop("sweep_type"),
                "status": d.pop("dispatch_status"),
                "issued_ts": d.pop("issued_ts"),
                "instruction": d.pop("instruction"),
                "segment_pod": d.pop("segment_pod"),
                "segment_target_pod": d.pop("segment_target_pod"),
            }
        else:
            for key in (
                "dispatch_id", "segment_id", "segment_name", "sweep_type",
                "dispatch_status", "issued_ts", "instruction",
                "segment_pod", "segment_target_pod",
            ):
                d.pop(key, None)

        d["latest_ping"] = latest_ping
        d["active_dispatch"] = active_dispatch
        result.append(d)
    return result


# Looks up one searcher by id or callsign.
# It adds a simple summary of that searcher's recent movement.
def get_searcher(id_or_callsign: int | str, mission_id: int | None = None) -> dict[str, Any]:
    """Return one searcher's status, active dispatch, and last-30-minute track summary."""
    mid = _resolve_mission_id(mission_id)
    searchers = list_searchers(mid)
    target: dict[str, Any] | None = None
    needle = str(id_or_callsign).strip()
    for searcher in searchers:
        if needle.isdigit() and searcher["id"] == int(needle):
            target = searcher
            break
        if searcher.get("callsign") and str(searcher["callsign"]).lower() == needle.lower():
            target = searcher
            break
    if target is None:
        raise ValueError(f"Searcher {id_or_callsign!r} not found in mission {mid}")

    since = int(time.time()) - 1800
    with session() as conn:
        rows = conn.execute(
            """
            SELECT ts, lat, lon
            FROM pings
            WHERE mission_id = ? AND user_id = ? AND ts >= ?
            ORDER BY ts ASC
            """,
            (mid, target["id"], since),
        ).fetchall()

    pts = [dict(row) for row in rows]
    distance_m = 0.0
    for a, b in zip(pts, pts[1:]):
        distance_m += _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    target["track_last_30m"] = {
        "ping_count": len(pts),
        "start_ts": pts[0]["ts"] if pts else None,
        "end_ts": pts[-1]["ts"] if pts else None,
        "distance_m": round(distance_m, 1),
    }
    return target


# Gets clues, sightings, hazards, and notes that searchers reported.
# Feeds the routing-agent pre-compute's "nearest clue" fact.
def get_findings(
    since_ts: int | None = None,
    kind: str | None = None,
    mission_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent field findings, newest first."""
    mid = _resolve_mission_id(mission_id)
    sql = """
        SELECT f.id, f.ts, f.kind, f.description, f.confidence,
               f.lat, f.lon, f.hex_id, u.callsign AS reporter_callsign,
               h.segment_id, s.name AS segment_name
        FROM findings f
        JOIN users u ON u.id = f.reporter_user_id
        JOIN hex_cells h ON h.id = f.hex_id
        LEFT JOIN segments s ON s.id = h.segment_id
        WHERE f.mission_id = ?
    """
    params: list[Any] = [mid]
    if since_ts is not None:
        sql += " AND f.ts >= ?"
        params.append(since_ts)
    if kind is not None:
        sql += " AND f.kind = ?"
        params.append(kind)
    sql += " ORDER BY f.ts DESC LIMIT ?"
    params.append(limit)
    with session() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


# Collects the newest things that happened in a mission.
# The routing worker uses this to decide whether to invoke the dispatcher.
def recent_events(
    mission_id: int | None = None,
    since_ts: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent dispatch/finding/broadcast/ping events for worker gating."""
    mid = _resolve_mission_id(mission_id)
    since = since_ts if since_ts is not None else int(time.time()) - 90
    events: list[dict[str, Any]] = []
    with session() as conn:
        dispatch_rows = conn.execute(
            """
            SELECT d.issued_ts AS ts, 'dispatch_issued' AS type, d.id,
                   u.callsign, d.status, d.segment_id, s.name AS segment_name,
                   d.instruction
            FROM dispatches d
            JOIN users u ON u.id = d.user_id
            LEFT JOIN segments s ON s.id = d.segment_id
            WHERE d.mission_id = ? AND d.issued_ts >= ?
            """,
            (mid, since),
        ).fetchall()
        finding_rows = conn.execute(
            """
            SELECT f.ts, 'finding_logged' AS type, f.id, f.kind,
                   u.callsign, f.description, f.confidence
            FROM findings f
            JOIN users u ON u.id = f.reporter_user_id
            WHERE f.mission_id = ? AND f.ts >= ?
            """,
            (mid, since),
        ).fetchall()
        broadcast_rows = conn.execute(
            """
            SELECT ts, 'broadcast' AS type, id, scope, kind, message
            FROM broadcasts
            WHERE mission_id = ? AND ts >= ?
            """,
            (mid, since),
        ).fetchall()
        ping_rows = conn.execute(
            """
            SELECT MAX(p.ts) AS ts, 'ping_activity' AS type,
                   COUNT(*) AS ping_count
            FROM pings p
            WHERE p.mission_id = ? AND p.ts >= ?
            HAVING COUNT(*) > 0
            """,
            (mid, since),
        ).fetchall()
    for rows in (dispatch_rows, finding_rows, broadcast_rows, ping_rows):
        events.extend(dict(row) for row in rows)
    events.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return events[:limit]
