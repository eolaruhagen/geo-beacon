"""Read-only agent skills for geo-beacon.

These functions are the agent's safe view of mission state. They summarize the
SQLite/SpatiaLite database at the segment/searcher level and deliberately avoid
exposing raw SQL or the full hex grid to the model.
"""
from __future__ import annotations

import math
import time
from typing import Any

from api.db import session
import api.db.dispatches as db_dispatches
import api.db.missions as db_missions
import api.db.pings as db_pings
from api.db.routing import snap_point_to_nearest_trail


ACTIVE_DISPATCH_STATUSES = ("pending", "acked", "in_progress")


# Measures the distance between two GPS dots.
# This helps us summarize how far a searcher walked.
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
# The worker uses this so it knows which missions the agent should think about.
def active_missions() -> list[dict[str, Any]]:
    """Return active missions, newest first. Used by the worker runner."""
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


# Gives the agent the big picture of one mission.
# It counts searchers, segments, and overall progress.
def get_mission_overview(mission_id: int | None = None) -> dict[str, Any]:
    """Return top-level mission counts and status."""
    mid = _resolve_mission_id(mission_id)
    with session() as conn:
        row = conn.execute(
            """
            SELECT m.id, m.name, m.status, m.subject_description,
                   m.pls_lat, m.pls_lon, m.pls_ts, m.started_ts, m.ended_ts,
                   COALESCE((
                     SELECT COUNT(*) FROM users u
                     WHERE u.current_mission_id = m.id AND u.role = 'searcher'
                   ), 0) AS total_searchers,
                   COALESCE((
                     SELECT COUNT(*) FROM users u
                     WHERE u.current_mission_id = m.id
                       AND u.role = 'searcher'
                       AND u.status IN ('dispatched', 'on_segment', 'returning')
                   ), 0) AS active_searchers,
                   COALESCE((
                     SELECT COUNT(*) FROM segments s WHERE s.mission_id = m.id
                   ), 0) AS total_segments,
                   COALESCE((
                     SELECT COUNT(*) FROM segments s
                     WHERE s.mission_id = m.id AND s.status IN ('swept', 'cleared')
                   ), 0) AS swept_segments,
                   COALESCE((
                     SELECT SUM(pos) FROM segments s WHERE s.mission_id = m.id
                   ), 0) AS cumulative_pos
            FROM missions m
            WHERE m.id = ?
            """,
            (mid,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Mission {mid} not found")
    return dict(row)


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


# Looks up one map segment by id or name.
# It tells the agent what is there, who is assigned, and what hazards touch it.
def get_segment(id_or_name: int | str, mission_id: int | None = None) -> dict[str, Any]:
    """Return a segment summary plus intersecting hazards and active assignments."""
    mid = _resolve_mission_id(mission_id)
    needle = str(id_or_name).strip()
    if needle.isdigit():
        where = "s.id = ?"
        params: tuple[Any, ...] = (int(needle), mid)
    else:
        where = "s.name = ?"
        params = (needle, mid)

    with session() as conn:
        row = conn.execute(
            f"""
            SELECT s.id, s.mission_id, s.name, s.area_m2, s.poa, s.pod, s.pos,
                   s.status, s.assigned_user_id, s.sweep_type, s.target_pod,
                   s.avg_slope_deg, s.dominant_cover, s.trail_length_m,
                   X(Centroid(s.geom)) AS center_lon,
                   Y(Centroid(s.geom)) AS center_lat
            FROM segments s
            WHERE {where} AND s.mission_id = ?
            """,
            params,
        ).fetchone()
        if row is None:
            raise ValueError(f"Segment {id_or_name!r} not found in mission {mid}")

        hazards = conn.execute(
            """
            SELECT h.id, h.kind, h.severity, h.description, h.created_ts, h.expires_ts
            FROM hazards h, segments s
            WHERE s.id = ? AND h.mission_id = ?
              AND ST_Intersects(s.geom, h.geom)
            ORDER BY h.severity DESC, h.created_ts DESC
            """,
            (row["id"], mid),
        ).fetchall()
        dispatches = conn.execute(
            """
            SELECT d.id, d.user_id, u.callsign, d.status, d.sweep_type,
                   d.issued_ts, d.instruction
            FROM dispatches d
            JOIN users u ON u.id = d.user_id
            WHERE d.segment_id = ?
              AND d.mission_id = ?
              AND d.status IN ('pending', 'acked', 'in_progress')
            ORDER BY d.issued_ts DESC
            """,
            (row["id"], mid),
        ).fetchall()

    result = dict(row)
    result["remaining_probability"] = round(float(result["poa"]) * (1.0 - float(result["pod"])), 6)
    result["hazards"] = [dict(h) for h in hazards]
    result["active_dispatches"] = [dict(d) for d in dispatches]
    return result


# Gets clues, sightings, hazards, footprints, and dropped items that searchers reported.
# The agent can filter by time or kind when it only wants recent important items.
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


# Summarizes the ground inside one segment.
# It counts things like steep cells, trails, searched cells, and danger flags.
def get_terrain_summary(segment_id: int, mission_id: int | None = None) -> dict[str, Any]:
    """Return terrain and coverage aggregate for one segment."""
    mid = _resolve_mission_id(mission_id)
    segment = get_segment(segment_id, mid)
    with session() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total_hexes,
                   AVG(slope_deg) AS avg_slope_deg,
                   MAX(slope_deg) AS max_slope_deg,
                   SUM(has_trail) AS trail_hexes,
                   SUM(has_road) AS road_hexes,
                   SUM(is_water) AS water_hexes,
                   SUM(is_building) AS building_hexes,
                   SUM(flag_danger) AS danger_hexes,
                   SUM(flag_impassable) AS impassable_hexes,
                   SUM(flag_clue) AS clue_hexes,
                   SUM(flag_searched) AS searched_hexes,
                   SUM(CASE WHEN is_water = 0 AND is_building = 0
                              AND flag_impassable = 0 THEN 1 ELSE 0 END)
                     AS searchable_hexes
            FROM hex_cells
            WHERE mission_id = ? AND segment_id = ?
            """,
            (mid, segment_id),
        ).fetchone()
    summary = dict(row) if row else {}
    summary.update({
        "mission_id": mid,
        "segment_id": segment["id"],
        "segment_name": segment["name"],
        "segment_status": segment["status"],
        "poa": segment["poa"],
        "pod": segment["pod"],
        "dominant_cover": segment["dominant_cover"],
        "trail_length_m": segment["trail_length_m"],
    })
    return summary


# Finds the best places to search next.
# It ranks segments by how much useful probability is still left there.
def get_uncovered_areas(
    min_poa: float = 0.0,
    mission_id: int | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Rank segments by remaining probability mass: POA * (1 - POD)."""
    mid = _resolve_mission_id(mission_id)
    with session() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.name, s.status, s.poa, s.pod, s.pos, s.sweep_type,
                   s.assigned_user_id, u.callsign AS assigned_callsign,
                   s.avg_slope_deg, s.dominant_cover, s.trail_length_m,
                   (s.poa * (1.0 - s.pod)) AS remaining_probability,
                   COALESCE((
                     SELECT COUNT(*) FROM hazards h
                     WHERE h.mission_id = s.mission_id
                       AND ST_Intersects(h.geom, s.geom)
                   ), 0) AS hazard_count
            FROM segments s
            LEFT JOIN users u ON u.id = s.assigned_user_id
            WHERE s.mission_id = ?
              AND s.poa >= ?
              AND s.status != 'cleared'
            ORDER BY remaining_probability DESC, s.poa DESC
            LIMIT ?
            """,
            (mid, min_poa, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# Makes a simple route hint between two GPS points.
# If trails exist, it bends the route through the nearest trail points.
def query_route(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    mission_id: int | None = None,
) -> dict[str, Any]:
    """Return snap-to-nearest-trail waypoints between two points."""
    mid = _resolve_mission_id(mission_id)
    snap_start = snap_point_to_nearest_trail(mid, from_lat, from_lon)
    snap_target = snap_point_to_nearest_trail(mid, to_lat, to_lon)
    waypoints = [{"lat": from_lat, "lon": from_lon}]
    snapped = False
    if snap_start is not None and snap_target is not None:
        waypoints.append({"lat": snap_start[0], "lon": snap_start[1]})
        waypoints.append({"lat": snap_target[0], "lon": snap_target[1]})
        snapped = True
    waypoints.append({"lat": to_lat, "lon": to_lon})
    return {"mission_id": mid, "waypoints": waypoints, "snapped": snapped}


# Collects the newest things that happened in a mission.
# The worker uses this to decide whether the agent needs to wake up.
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


# Builds the main mission story the agent reads first.
# This is a convenience tool so OpenClaw can ask for the brief again.
def get_mission_brief(mission_id: int | None = None) -> str:
    """Return the deterministic mission brief that the worker gives OpenClaw."""
    from agent.brief import compose_brief

    return compose_brief(mission_id=mission_id)
