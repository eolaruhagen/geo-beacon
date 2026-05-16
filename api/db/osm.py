from __future__ import annotations

import json

from api.db import session

# OSMFeature keys: kind ('trail'|'road'|'water'|'building'),
#                  name (optional), geom_geojson (Polygon or LineString)
OSMFeature = dict


def bulk_insert_osm_features(mission_id: int, features: list[OSMFeature]) -> int:
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
