from __future__ import annotations

import json
import time

from api.db import session


def create_mission(
    name: str,
    subject_description: str,
    pls_lat: float,
    pls_lon: float,
    pls_ts: int,
    area_geojson: dict,
    created_by_user_id: int,
    join_code: str,
) -> int:
    """Insert mission row. status='planning'. started_ts=now. Returns mission_id."""
    now = int(time.time())
    with session() as conn:
        cur = conn.execute(
            """
            INSERT INTO missions (name, status, subject_description, pls_lat, pls_lon,
                                  pls_ts, started_ts, created_by_user_id, join_code, area_geom)
            VALUES (?, 'planning', ?, ?, ?, ?, ?, ?, ?,
                    SetSRID(GeomFromGeoJSON(?), 4326))
            """,
            (name, subject_description, pls_lat, pls_lon, pls_ts, now,
             created_by_user_id, join_code, json.dumps(area_geojson)),
        )
        return cur.lastrowid


def get_mission(mission_id: int) -> dict | None:
    """All columns plus area_geom as GeoJSON dict (key 'area_geojson')."""
    with session() as conn:
        row = conn.execute(
            """
            SELECT id, name, status, subject_description, pls_lat, pls_lon, pls_ts,
                   started_ts, ended_ts, join_code, created_by_user_id,
                   AsGeoJSON(area_geom) AS area_geojson
            FROM missions WHERE id = ?
            """,
            (mission_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d["area_geojson"] is not None:
            d["area_geojson"] = json.loads(d["area_geojson"])
        return d


def get_mission_by_join_code(join_code: str) -> dict | None:
    with session() as conn:
        row = conn.execute(
            """
            SELECT id, name, status, subject_description, pls_lat, pls_lon, pls_ts,
                   started_ts, ended_ts, join_code, created_by_user_id,
                   AsGeoJSON(area_geom) AS area_geojson
            FROM missions WHERE join_code = ?
            """,
            (join_code,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d["area_geojson"] is not None:
            d["area_geojson"] = json.loads(d["area_geojson"])
        return d


def set_status(mission_id: int, status: str) -> None:
    """Update mission.status. Sets ended_ts when transitioning to ended."""
    now = int(time.time())
    with session() as conn:
        if status == "ended":
            conn.execute(
                "UPDATE missions SET status = ?, ended_ts = ? WHERE id = ?",
                (status, now, mission_id),
            )
        else:
            conn.execute(
                "UPDATE missions SET status = ? WHERE id = ?",
                (status, mission_id),
            )


def active_mission_id_for_user(user_id: int) -> int | None:
    """Returns mission_id of the mission this user is currently associated with.

    Lookup order (migration 004 added users.current_mission_id, which is the
    authoritative source now):
      1. users.current_mission_id direct read — set when the user creates or
         joins a mission.
      2. Defensive fallback: mission this user created (created_by_user_id)
         OR most recent mission this user has pinged into. Kept around in case
         a legacy row has current_mission_id NULL.
      3. Final fallback: the single mission with status='active', if exactly
         one exists. Matches the single-active-mission scope per spec §2.
    """
    with session() as conn:
        row = conn.execute(
            "SELECT current_mission_id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is not None and row["current_mission_id"] is not None:
            return row["current_mission_id"]

        row = conn.execute(
            """
            SELECT id FROM missions
            WHERE created_by_user_id = ?
            UNION
            SELECT DISTINCT mission_id AS id FROM pings
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, user_id),
        ).fetchone()
        if row:
            return row["id"]
        active = conn.execute(
            "SELECT id FROM missions WHERE status = 'active'"
        ).fetchall()
        if len(active) == 1:
            return active[0]["id"]
        return None
