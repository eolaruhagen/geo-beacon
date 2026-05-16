from __future__ import annotations

import json

from api.db import session


def bulk_insert_segments(mission_id: int, rows: list[dict]) -> list[int]:
    """Insert all rows in one transaction. status='unassigned'.
    Returns list of inserted ids in the same order as input rows."""
    ids: list[int] = []
    with session() as conn:
        conn.execute("BEGIN")
        for row in rows:
            cur = conn.execute(
                """
                INSERT INTO segments (mission_id, name, area_m2, poa, pod, pos,
                                      status, avg_slope_deg, dominant_cover,
                                      trail_length_m, geom)
                VALUES (?, ?, ?, ?, 0, 0, 'unassigned', ?, ?, ?,
                        SetSRID(GeomFromGeoJSON(?), 4326))
                """,
                (
                    mission_id,
                    row["name"],
                    row["area_m2"],
                    row["poa"],
                    row["avg_slope_deg"],
                    row["dominant_cover"],
                    row.get("trail_length_m", 0.0),
                    json.dumps(row["poly_geojson"]),
                ),
            )
            ids.append(cur.lastrowid)
        conn.execute("COMMIT")
        return ids


CRITICAL_POA_FACTOR = 0.0
CAUTION_POA_FACTOR = 0.3


def apply_hazard_penalty(mission_id: int) -> dict[str, int]:
    """For every segment whose geometry intersects a hazard, multiply POA by a
    severity-based factor (critical → 0, caution → 0.3), then renormalize so
    Σ poa across the mission = 1. Call AFTER both segments and hazards are
    seeded. Returns counts {critical_zeroed, caution_penalized}."""
    with session() as conn:
        critical = conn.execute(
            """
            UPDATE segments SET poa = poa * ?
            WHERE mission_id = ? AND id IN (
              SELECT s.id FROM segments s, hazards h
              WHERE s.mission_id = ? AND h.mission_id = ?
                AND h.severity = 'critical'
                AND ST_Intersects(s.geom, h.geom)
            )
            """,
            (CRITICAL_POA_FACTOR, mission_id, mission_id, mission_id),
        ).rowcount

        caution = conn.execute(
            """
            UPDATE segments SET poa = poa * ?
            WHERE mission_id = ?
              AND poa > 0
              AND id IN (
                SELECT s.id FROM segments s, hazards h
                WHERE s.mission_id = ? AND h.mission_id = ?
                  AND h.severity = 'caution'
                  AND ST_Intersects(s.geom, h.geom)
              )
            """,
            (CAUTION_POA_FACTOR, mission_id, mission_id, mission_id),
        ).rowcount

        # Renormalize so Σ poa = 1 across the mission. If everything is zero
        # (degenerate), leave as zero rather than dividing by zero.
        total_row = conn.execute(
            "SELECT SUM(poa) AS s FROM segments WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        total = float(total_row["s"]) if total_row and total_row["s"] else 0.0
        if total > 0:
            conn.execute(
                "UPDATE segments SET poa = poa / ? WHERE mission_id = ?",
                (total, mission_id),
            )

    return {
        "critical_zeroed": critical or 0,
        "caution_penalized": caution or 0,
    }


def segments_for_mission(mission_id: int) -> list[dict]:
    """All columns plus geom as GeoJSON dict (key 'geom_geojson')."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT id, mission_id, name, area_m2, poa, pod, pos, status,
                   sweep_type, target_pod, avg_slope_deg, dominant_cover,
                   trail_length_m, assigned_user_id, AsGeoJSON(geom) AS geom_geojson
            FROM segments WHERE mission_id = ?
            """,
            (mission_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d["geom_geojson"] is not None:
                d["geom_geojson"] = json.loads(d["geom_geojson"])
            result.append(d)
        return result
