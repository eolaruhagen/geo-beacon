#!/usr/bin/env python3
"""Derive non-dynamic hazards from terrain + OSM at mission init.

Sources (all at init time, not agent-driven):
  - osm_features kind='water'    → hazards kind='water',  severity='critical'
  - osm_features kind='road'     → hazards kind='other',  severity='caution' (buffered ~5m)
  - osm_features kind='building' → hazards kind='other',  severity='caution' (buffered ~2m)
  - terrain_cells avg_slope_deg ≥ CLIFF_SLOPE_DEG → hazards kind='cliff', severity='caution'

Idempotent: deletes existing hazards for the mission before inserting.

Usage:
    python scripts/seed_hazards.py --mission-id N [--verbose]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import session
from api.db.hazards import bulk_insert_hazards, delete_hazards_for_mission

log = logging.getLogger("seed_hazards")

CLIFF_SLOPE_DEG = 30.0
ROAD_BUFFER_M = 5.0
BUILDING_BUFFER_M = 2.0


def _buffer_deg_for_meters(lat: float, meters: float) -> float:
    """Approximate degrees for ST_Buffer at given latitude. Buffer is in degrees
    because our geometries are EPSG:4326."""
    deg_per_meter_lat = 1.0 / 111_320.0
    deg_per_meter_lon = 1.0 / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
    return meters * (deg_per_meter_lat + deg_per_meter_lon) / 2


def _mission_centroid_lat(mission_id: int) -> float:
    with session() as conn:
        row = conn.execute(
            "SELECT Y(Centroid(area_geom)) AS lat FROM missions WHERE id = ?",
            (mission_id,),
        ).fetchone()
        return float(row["lat"]) if row and row["lat"] is not None else 37.0


def seed_hazards(mission_id: int) -> dict[str, int]:
    """Insert structural hazards for a mission.

    Returns a counts dict {water, road, building, cliff, total}."""
    counts = {"water": 0, "road": 0, "building": 0, "cliff": 0}
    delete_hazards_for_mission(mission_id)

    centroid_lat = _mission_centroid_lat(mission_id)
    road_buf = _buffer_deg_for_meters(centroid_lat, ROAD_BUFFER_M)
    bldg_buf = _buffer_deg_for_meters(centroid_lat, BUILDING_BUFFER_M)

    rows: list[dict] = []

    with session() as conn:
        # OSM water → critical water hazards (one row per polygon part)
        water_rows = conn.execute(
            """
            SELECT name, AsGeoJSON(geom) AS gj
            FROM osm_features WHERE mission_id = ? AND kind = 'water'
            """,
            (mission_id,),
        ).fetchall()
        for w in water_rows:
            gj = json.loads(w["gj"]) if w["gj"] else None
            if not gj:
                continue
            for poly in _explode_to_polygons(gj):
                rows.append({
                    "kind": "water",
                    "severity": "critical",
                    "description": f"Water body: {w['name'] or 'unnamed'} — drowning risk",
                    "poly_geojson": poly,
                })
                counts["water"] += 1

        # OSM road → caution 'other' hazards (buffered to a polygon)
        road_rows = conn.execute(
            """
            SELECT name, AsGeoJSON(ST_Buffer(geom, ?)) AS gj
            FROM osm_features WHERE mission_id = ? AND kind = 'road'
            """,
            (road_buf, mission_id),
        ).fetchall()
        for r in road_rows:
            gj = json.loads(r["gj"]) if r["gj"] else None
            if not gj:
                continue
            for poly in _explode_to_polygons(gj):
                rows.append({
                    "kind": "other",
                    "severity": "caution",
                    "description": f"Road corridor: {r['name'] or 'unnamed'} — vehicle traffic",
                    "poly_geojson": poly,
                })
                counts["road"] += 1

        # OSM building → caution 'other' hazards (buffered slightly)
        bldg_rows = conn.execute(
            """
            SELECT name, AsGeoJSON(ST_Buffer(geom, ?)) AS gj
            FROM osm_features WHERE mission_id = ? AND kind = 'building'
            """,
            (bldg_buf, mission_id),
        ).fetchall()
        for b in bldg_rows:
            gj = json.loads(b["gj"]) if b["gj"] else None
            if not gj:
                continue
            for poly in _explode_to_polygons(gj):
                rows.append({
                    "kind": "other",
                    "severity": "caution",
                    "description": f"Structure: {b['name'] or 'unnamed'} — impassable / private",
                    "poly_geojson": poly,
                })
                counts["building"] += 1

        # Steep terrain → cliff hazards (per-cell)
        cliff_rows = conn.execute(
            """
            SELECT avg_slope_deg, AsGeoJSON(geom) AS gj
            FROM terrain_cells
            WHERE mission_id = ? AND avg_slope_deg >= ?
            """,
            (mission_id, CLIFF_SLOPE_DEG),
        ).fetchall()
        for c in cliff_rows:
            gj = json.loads(c["gj"]) if c["gj"] else None
            if not gj:
                continue
            for poly in _explode_to_polygons(gj):
                rows.append({
                    "kind": "cliff",
                    "severity": "caution",
                    "description": f"Steep terrain ({c['avg_slope_deg']:.0f}° slope) — fall risk",
                    "poly_geojson": poly,
                })
                counts["cliff"] += 1

    inserted = bulk_insert_hazards(mission_id, rows) if rows else 0
    counts["total"] = inserted
    log.info(
        "Seeded hazards for mission %d: water=%d road=%d building=%d cliff=%d (total=%d)",
        mission_id, counts["water"], counts["road"], counts["building"], counts["cliff"], inserted,
    )
    return counts


def _explode_to_polygons(geom: dict) -> list[dict]:
    """The hazards.geom column is POLYGON-only. Flatten MultiPolygons to a list
    of Polygons. LineString/Point inputs are filtered out (caller is responsible
    for buffering)."""
    t = geom.get("type")
    if t == "Polygon":
        return [geom]
    if t == "MultiPolygon":
        return [{"type": "Polygon", "coordinates": part} for part in geom["coordinates"]]
    return []


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mission-id", type=int, required=True)
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    print(f"[seed_hazards] mission_id={args.mission_id}", flush=True)
    try:
        counts = seed_hazards(args.mission_id)
    except Exception as exc:
        print(f"[seed_hazards] ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"[seed_hazards] done: {counts}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
