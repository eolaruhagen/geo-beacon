from __future__ import annotations

import json
import time

from api.db import session


def mission_state_feature_collection(mission_id: int) -> dict:
    """Returns full GeoJSON FeatureCollection per spec §11."""
    features = []
    thirty_min_ago = int(time.time()) - 1800

    with session() as conn:
        # Segments: Polygon Features
        seg_rows = conn.execute(
            """
            SELECT id, name, poa, pod, pos, status, sweep_type, assigned_user_id,
                   AsGeoJSON(geom) AS geom_json
            FROM segments WHERE mission_id = ?
            """,
            (mission_id,),
        ).fetchall()
        for row in seg_rows:
            geom = json.loads(row["geom_json"]) if row["geom_json"] else None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "feature_type": "segment",
                    "id": row["id"],
                    "name": row["name"],
                    "poa": row["poa"],
                    "pod": row["pod"],
                    "pos": row["pos"],
                    "status": row["status"],
                    "sweep_type": row["sweep_type"],
                    "assigned_user_id": row["assigned_user_id"],
                },
            })

        # Hex cells: non-default flags only
        hex_rows = conn.execute(
            """
            SELECT id, flag_danger, flag_impassable, flag_clue, flag_poi,
                   is_water, is_building, AsGeoJSON(geom) AS geom_json
            FROM hex_cells
            WHERE mission_id = ?
              AND (flag_danger = 1 OR flag_impassable = 1 OR flag_clue = 1
                   OR flag_poi = 1 OR is_water = 1 OR is_building = 1)
            """,
            (mission_id,),
        ).fetchall()
        for row in hex_rows:
            geom = json.loads(row["geom_json"]) if row["geom_json"] else None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "feature_type": "hex_cell",
                    "id": row["id"],
                    "flag_danger": row["flag_danger"],
                    "flag_impassable": row["flag_impassable"],
                    "flag_clue": row["flag_clue"],
                    "flag_poi": row["flag_poi"],
                    "is_water": row["is_water"],
                    "is_building": row["is_building"],
                },
            })

        # Searchers: latest ping per user as Point Feature
        searcher_rows = conn.execute(
            """
            SELECT u.id AS user_id, u.callsign, u.status,
                   AsGeoJSON(p.geom) AS geom_json
            FROM users u
            JOIN pings p ON p.id = (
                SELECT id FROM pings
                WHERE user_id = u.id AND mission_id = ?
                ORDER BY ts DESC LIMIT 1
            )
            WHERE u.role IN ('searcher', 'observer')
            """,
            (mission_id,),
        ).fetchall()
        for row in searcher_rows:
            geom = json.loads(row["geom_json"]) if row["geom_json"] else None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "feature_type": "searcher",
                    "user_id": row["user_id"],
                    "callsign": row["callsign"],
                    "status": row["status"],
                },
            })

        # Tracks: last-30-min pings per searcher as LineString
        track_rows = conn.execute(
            """
            SELECT user_id, AsGeoJSON(MakeLine(geom)) AS line_json
            FROM (
                SELECT user_id, geom FROM pings
                WHERE mission_id = ? AND ts >= ?
                ORDER BY user_id, ts
            )
            GROUP BY user_id
            HAVING COUNT(*) >= 2
            """,
            (mission_id, thirty_min_ago),
        ).fetchall()
        for row in track_rows:
            geom = json.loads(row["line_json"]) if row["line_json"] else None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "feature_type": "track",
                    "user_id": row["user_id"],
                },
            })

        # Findings: Point Features
        finding_rows = conn.execute(
            """
            SELECT id, kind, description, confidence, ts,
                   AsGeoJSON(geom) AS geom_json
            FROM findings WHERE mission_id = ?
            """,
            (mission_id,),
        ).fetchall()
        for row in finding_rows:
            geom = json.loads(row["geom_json"]) if row["geom_json"] else None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "feature_type": "finding",
                    "kind": row["kind"],
                    "description": row["description"],
                    "confidence": row["confidence"],
                    "ts": row["ts"],
                },
            })

        # Hazards: Polygon Features
        hazard_rows = conn.execute(
            """
            SELECT id, kind, severity, description,
                   AsGeoJSON(geom) AS geom_json
            FROM hazards WHERE mission_id = ?
            """,
            (mission_id,),
        ).fetchall()
        for row in hazard_rows:
            geom = json.loads(row["geom_json"]) if row["geom_json"] else None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "feature_type": "hazard",
                    "id": row["id"],
                    "kind": row["kind"],
                    "severity": row["severity"],
                    "description": row["description"],
                },
            })

        # OSM features: LineString/Polygon
        osm_rows = conn.execute(
            """
            SELECT id, kind, name, AsGeoJSON(geom) AS geom_json
            FROM osm_features WHERE mission_id = ?
            """,
            (mission_id,),
        ).fetchall()
        for row in osm_rows:
            geom = json.loads(row["geom_json"]) if row["geom_json"] else None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "feature_type": "osm_feature",
                    "kind": row["kind"],
                    "name": row["name"],
                },
            })

    return {"type": "FeatureCollection", "features": features}
