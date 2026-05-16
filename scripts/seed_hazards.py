#!/usr/bin/env python3
"""Derive structural hazards from OSM features + hex_cells at mission init.

Hazards are areas that are *risky to enter*. They penalize segment POA
(critical → ×0, caution → ×0.3) and set flag_danger on intersecting hexes.

Sources:
  - osm_features kind='water'    → hazard kind='water', severity='critical'  (drowning)
  - hex_cells.slope_deg >= 30    → kind='cliff',        severity='caution'   (fall risk)

Buildings and roads are intentionally NOT hazards. They're descriptive — a
building isn't risky to walk near, and a road corridor isn't dangerous to
search around. They're surfaced via the feature-type flags has_road and
is_building (set on hex_cells), which renderers can style differently without
ever entering the POA penalty / flag_danger path.

After inserting hazards, rasterizes each to hex_cells.flag_danger. Also sets
is_water=1 and is_building=1 on hex_cells inside water/building OSM polygons
(feature-type flags only — no POA impact for buildings).

Idempotent: deletes existing hazards and resets hex_cells flags before re-deriving.

Usage:
    python scripts/seed_hazards.py --mission-id N [--verbose]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import session
from api.db.hazards import bulk_insert_hazards, delete_hazards_for_mission
from api.db.hex_cells import rasterize_hazard_to_hex_flags

log = logging.getLogger("seed_hazards")

CLIFF_SLOPE_DEG = 30.0


def _explode_to_polygons(geom: dict) -> list[dict]:
    t = geom.get("type")
    if t == "Polygon":
        return [geom]
    if t == "MultiPolygon":
        return [{"type": "Polygon", "coordinates": part} for part in geom["coordinates"]]
    return []


def seed_hazards(mission_id: int) -> dict[str, int]:
    """Insert structural hazards, rasterize to hex_cells, set feature-type flags.

    Returns counts {water, cliff, total_hazards,
                    hexes_flagged_danger, hexes_flagged_water, hexes_flagged_building}.
    """
    counts = {
        "water": 0, "cliff": 0,
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

        # Roads and buildings are NOT inserted as hazards. They're feature-type
        # information surfaced via has_road / is_building flags on hex_cells
        # (set elsewhere — has_road in fetch_terrain.py, is_building below).
        # A building or road corridor isn't *risky to enter*; treating them as
        # caution hazards used to zero ~70% of POA off any segment that
        # clipped a structure or road, which doesn't match the actual SAR
        # search model.

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
        "Seeded hazards for mission %d: water=%d cliff=%d (total=%d) "
        "flagged_danger=%d flagged_water=%d flagged_building=%d",
        mission_id,
        counts["water"], counts["cliff"],
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
