#!/usr/bin/env python3
"""Derive structural hazards from OSM features + hex_cells at mission init.

Sources:
  - osm_features kind='water'    → hazard kind='water',  severity='critical'
  - osm_features kind='road'     → kind='other', 'caution', buffered 5m
  - osm_features kind='building' → kind='other', 'caution', buffered 2m
  - hex_cells.slope_deg >= 30    → kind='cliff', severity='caution'

After inserting hazards, rasterizes each to hex_cells.flag_danger.
Also sets is_water=1 / is_building=1 on hex_cells inside water/building OSM polygons.

Idempotent: deletes existing hazards and resets hex_cells flags before re-deriving.

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
from api.db.hex_cells import rasterize_hazard_to_hex_flags

log = logging.getLogger("seed_hazards")

CLIFF_SLOPE_DEG = 30.0
ROAD_BUFFER_M = 5.0
BUILDING_BUFFER_M = 2.0


def _buffer_deg(lat: float, meters: float) -> float:
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


def _explode_to_polygons(geom: dict) -> list[dict]:
    t = geom.get("type")
    if t == "Polygon":
        return [geom]
    if t == "MultiPolygon":
        return [{"type": "Polygon", "coordinates": part} for part in geom["coordinates"]]
    return []


def seed_hazards(mission_id: int) -> dict[str, int]:
    """Insert structural hazards, rasterize to hex_cells, set feature-type flags.

    Returns counts {water, road, building, cliff, total_hazards,
                    hexes_flagged_danger, hexes_flagged_water, hexes_flagged_building}.
    """
    counts = {
        "water": 0, "road": 0, "building": 0, "cliff": 0,
        "total_hazards": 0,
        "hexes_flagged_danger": 0,
        "hexes_flagged_water": 0,
        "hexes_flagged_building": 0,
    }

    # Idempotent: clear existing hazards and reset hex flags for this mission
    delete_hazards_for_mission(mission_id)
    with session() as conn:
        conn.execute(
            "UPDATE hex_cells SET flag_danger=0, is_water=0, is_building=0 WHERE mission_id=?",
            (mission_id,),
        )

    centroid_lat = _mission_centroid_lat(mission_id)
    road_buf = _buffer_deg(centroid_lat, ROAD_BUFFER_M)
    bldg_buf = _buffer_deg(centroid_lat, BUILDING_BUFFER_M)

    rows: list[dict] = []

    with session() as conn:
        # Water → critical hazards
        water_rows = conn.execute(
            "SELECT name, AsGeoJSON(geom) AS gj FROM osm_features WHERE mission_id=? AND kind='water'",
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

        # Road → caution 'other' hazards (buffered)
        road_rows = conn.execute(
            "SELECT name, AsGeoJSON(ST_Buffer(geom, ?)) AS gj FROM osm_features WHERE mission_id=? AND kind='road'",
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

        # Building → caution 'other' hazards (buffered)
        bldg_rows = conn.execute(
            "SELECT name, AsGeoJSON(ST_Buffer(geom, ?)) AS gj FROM osm_features WHERE mission_id=? AND kind='building'",
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

        # Steep hex_cells → cliff hazards (per cell, fall back to per-cell if component logic is hard)
        cliff_rows = conn.execute(
            "SELECT slope_deg, AsGeoJSON(geom) AS gj FROM hex_cells WHERE mission_id=? AND slope_deg >= ?",
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
                    "description": f"Steep terrain ({c['slope_deg']:.0f}° slope) — fall risk",
                    "poly_geojson": poly,
                })
                counts["cliff"] += 1

    # Insert all hazard rows, get back their ids for rasterization
    hazard_ids: list[int] = []
    if rows:
        hazard_ids = bulk_insert_hazards(mission_id, rows)

    counts["total_hazards"] = len(hazard_ids)

    # Rasterize each hazard → flag_danger on intersecting hex_cells
    for h_id in hazard_ids:
        n_flagged = rasterize_hazard_to_hex_flags(mission_id, h_id)
        counts["hexes_flagged_danger"] += n_flagged

    # Set is_water=1 on hex_cells inside water OSM polygons (feature-type flag, not hazard)
    with session() as conn:
        water_result = conn.execute(
            """
            UPDATE hex_cells
            SET is_water = 1
            WHERE mission_id = ?
              AND EXISTS (
                SELECT 1 FROM osm_features f
                WHERE f.mission_id = hex_cells.mission_id
                  AND f.kind = 'water'
                  AND ST_Contains(f.geom, hex_cells.geom)
              )
            """,
            (mission_id,),
        )
        counts["hexes_flagged_water"] = water_result.rowcount

        building_result = conn.execute(
            """
            UPDATE hex_cells
            SET is_building = 1
            WHERE mission_id = ?
              AND EXISTS (
                SELECT 1 FROM osm_features f
                WHERE f.mission_id = hex_cells.mission_id
                  AND f.kind = 'building'
                  AND ST_Contains(f.geom, hex_cells.geom)
              )
            """,
            (mission_id,),
        )
        counts["hexes_flagged_building"] = building_result.rowcount

    log.info(
        "Seeded hazards for mission %d: water=%d road=%d building=%d cliff=%d "
        "(total=%d) flagged_danger=%d flagged_water=%d flagged_building=%d",
        mission_id,
        counts["water"], counts["road"], counts["building"], counts["cliff"],
        counts["total_hazards"],
        counts["hexes_flagged_danger"],
        counts["hexes_flagged_water"],
        counts["hexes_flagged_building"],
    )
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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
