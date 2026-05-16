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
) -> int:
    """Insert mission row with area_geom from GeoJSON. status='planning'.
    Returns new mission_id. Sets started_ts = now."""
    now = int(time.time())
    with session() as conn:
        cur = conn.execute(
            """
            INSERT INTO missions (name, status, subject_description, pls_lat, pls_lon,
                                  pls_ts, started_ts, area_geom)
            VALUES (?, 'planning', ?, ?, ?, ?, ?,
                    SetSRID(GeomFromGeoJSON(?), 4326))
            """,
            (name, subject_description, pls_lat, pls_lon, pls_ts, now,
             json.dumps(area_geojson)),
        )
        return cur.lastrowid


def get_mission(mission_id: int) -> dict | None:
    """All columns plus area_geom as GeoJSON dict (key 'area_geojson')."""
    with session() as conn:
        row = conn.execute(
            """
            SELECT id, name, status, subject_description, pls_lat, pls_lon, pls_ts,
                   started_ts, ended_ts, AsGeoJSON(area_geom) AS area_geojson
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


def active_mission_id() -> int | None:
    """Returns id of the single mission with status='active', else None."""
    with session() as conn:
        row = conn.execute(
            "SELECT id FROM missions WHERE status = 'active' LIMIT 1"
        ).fetchone()
        return row["id"] if row else None
