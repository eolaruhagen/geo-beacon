from __future__ import annotations

import json

from api.db import session


def bulk_insert_terrain_cells(mission_id: int, cells: list[dict]) -> int:
    """Insert terrain cells in one transaction. Returns count inserted."""
    with session() as conn:
        conn.execute("BEGIN")
        for cell in cells:
            conn.execute(
                """
                INSERT INTO terrain_cells (mission_id, center_elev_m, avg_slope_deg,
                                           dominant_cover, geom)
                VALUES (?, ?, ?, ?, SetSRID(GeomFromGeoJSON(?), 4326))
                """,
                (
                    mission_id,
                    cell["center_elev_m"],
                    cell["avg_slope_deg"],
                    cell["dominant_cover"],
                    json.dumps(cell["poly_geojson"]),
                ),
            )
        conn.execute("COMMIT")
        return len(cells)


def bulk_insert_osm_features(mission_id: int, features: list[dict]) -> int:
    """Insert OSM features in one transaction. Returns count inserted."""
    with session() as conn:
        conn.execute("BEGIN")
        for feat in features:
            conn.execute(
                """
                INSERT INTO osm_features (mission_id, kind, name, geom)
                VALUES (?, ?, ?, SetSRID(GeomFromGeoJSON(?), 4326))
                """,
                (
                    mission_id,
                    feat["kind"],
                    feat.get("name"),
                    json.dumps(feat["geom_geojson"]),
                ),
            )
        conn.execute("COMMIT")
        return len(features)


def terrain_cells_for_mission(mission_id: int) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            """
            SELECT id, mission_id, center_elev_m, avg_slope_deg, dominant_cover,
                   AsGeoJSON(geom) AS poly_geojson
            FROM terrain_cells WHERE mission_id = ?
            """,
            (mission_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d["poly_geojson"] is not None:
                d["poly_geojson"] = json.loads(d["poly_geojson"])
            result.append(d)
        return result


def osm_features_for_mission(mission_id: int) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            """
            SELECT id, mission_id, kind, name, AsGeoJSON(geom) AS geom_geojson
            FROM osm_features WHERE mission_id = ?
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
