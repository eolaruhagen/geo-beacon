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
    """Returns the user's currently-affiliated mission id, or None.

    Authoritative source: users.current_mission_id. Set when the user creates
    a mission (api/routes/missions.py:POST /missions) or joins one
    (api/routes/missions.py:POST /missions/join).

    AUTH-2: the previous "single active mission" global fallback and the
    created-by/pings inference fallback were removed. They let an unaffiliated
    user implicitly land in someone else's mission as soon as exactly one
    mission was live — a real auth hole given the demo deploy has exactly one
    active mission at a time. If a user has NULL current_mission_id, the
    correct response is "no active mission" (None), forcing the caller to
    surface a 409 / 404.
    """
    with session() as conn:
        row = conn.execute(
            "SELECT current_mission_id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is not None and row["current_mission_id"] is not None:
            return row["current_mission_id"]
        return None
