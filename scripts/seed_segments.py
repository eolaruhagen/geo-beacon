#!/usr/bin/env python3
"""Subdivide a mission area into ~105m flat-to-flat hex segments and compute
initial POA.

Usage:
    python scripts/seed_segments.py --mission-id N [--verbose]

Reads mission from DB, generates a flat-top hex grid over the mission bbox
(odd-q offset coordinates, origin = bbox SW corner), aggregates fine-hex
terrain stats from `hex_data` passed in memory, computes POA, bulk-inserts
segments.

Grid choices:
  * Cell shape: flat-top hexagon, 105 m flat-to-flat (~9,545 m² each).
  * Origin: bbox SW corner of the mission area. NOT clipped to area_geom —
    hexes covering the bbox stay even if they stick out beyond the user-drawn
    polygon (matches the previous square-grid behavior).
  * Naming: row/col offset, formatted `S-r{row:02d}-c{col:02d}`, so callouts
    over radio map directly to a grid coordinate.
  * Fine-hex → coarse-hex assignment for POA aggregation: nearest coarse-hex
    center (equivalent to point-in-polygon for a regular hex grid). The
    `seed_hex_cells` step does the on-DB assignment via SpatiaLite
    ST_Contains, which agrees with this on the interior; the two only
    disagree for degenerate centroid-on-edge cases that don't affect stats.

New signature: seed_segments(mission_id, hex_data) -> list[int]
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import session
from api.db.missions import get_mission
from api.db.segments import bulk_insert_segments
from agent.poa import initial_poa_weights

log = logging.getLogger("seed_segments")


# ---------------------------------------------------------------------------
# Grid parameters
# ---------------------------------------------------------------------------

CELL_FLAT_TO_FLAT_M = 105.0
W = CELL_FLAT_TO_FLAT_M
R = W / math.sqrt(3)          # circumradius / side length
COL_STEP_M = 1.5 * R          # ≈ W * sqrt(3)/2 ≈ 90.9 m
ROW_STEP_M = W                # ≈ 105 m
COL_Y_OFFSET_M = W / 2        # vertical shift for odd columns
HEX_AREA_M2 = (3.0 * math.sqrt(3.0) / 2.0) * R * R


# ---------------------------------------------------------------------------
# Projection helpers (equirectangular, anchored at the mission centroid)
# ---------------------------------------------------------------------------

def _make_projection(lat0: float, lon0: float):
    cos_lat0 = math.cos(math.radians(lat0))
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * cos_lat0

    def to_xy(lat: float, lon: float) -> tuple[float, float]:
        return ((lon - lon0) * m_per_deg_lon, (lat - lat0) * m_per_deg_lat)

    def to_latlon(x: float, y: float) -> tuple[float, float]:
        return (lat0 + y / m_per_deg_lat, lon0 + x / m_per_deg_lon)

    return to_xy, to_latlon


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _bbox_from_geojson(geojson: dict) -> tuple[float, float, float, float]:
    coords = geojson["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lats), min(lons), max(lats), max(lons)


def _flat_top_hex_vertices_m(cx: float, cy: float) -> list[tuple[float, float]]:
    """CCW vertices of a flat-top hex centered at (cx, cy). Closed ring (7 points)."""
    h = W / 2.0  # apothem
    return [
        (cx + R,       cy),
        (cx + R / 2,   cy + h),
        (cx - R / 2,   cy + h),
        (cx - R,       cy),
        (cx - R / 2,   cy - h),
        (cx + R / 2,   cy - h),
        (cx + R,       cy),  # close
    ]


def _generate_hex_grid(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float, to_xy, to_latlon,
) -> list[dict]:
    """Generate the coarse hex grid covering the bbox.

    Returns a list of dicts (in deterministic row-major order):
      {row, col, center_lat, center_lon, center_x, center_y, poly_geojson}
    """
    min_x, min_y = to_xy(min_lat, min_lon)
    max_x, max_y = to_xy(max_lat, max_lon)

    # +1 in each axis to fully cover the bbox; an off-by-one here only adds
    # an extra rim of hexes and never drops one.
    ncols = int(math.ceil((max_x - min_x) / COL_STEP_M)) + 1
    nrows = int(math.ceil((max_y - min_y) / ROW_STEP_M)) + 1

    cells: list[dict] = []
    for col in range(ncols):
        cx = min_x + col * COL_STEP_M
        for row in range(nrows):
            cy = min_y + row * ROW_STEP_M
            if col % 2 == 1:
                cy += COL_Y_OFFSET_M

            verts_m = _flat_top_hex_vertices_m(cx, cy)
            ring = []
            for vx, vy in verts_m:
                vlat, vlon = to_latlon(vx, vy)
                ring.append([vlon, vlat])  # GeoJSON is [lon, lat]

            center_lat, center_lon = to_latlon(cx, cy)
            cells.append({
                "row": row,
                "col": col,
                "center_x": cx,
                "center_y": cy,
                "center_lat": center_lat,
                "center_lon": center_lon,
                "poly_geojson": {"type": "Polygon", "coordinates": [ring]},
            })
    return cells


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _assign_fine_to_coarse(
    fine_hexes: list[dict], coarse: list[dict], to_xy,
) -> dict[int, list[dict]]:
    """Group fine hexes by the index of the coarse hex whose center is nearest
    (equivalent to point-in-polygon for a regular hex grid)."""
    groups: dict[int, list[dict]] = defaultdict(list)
    for fh in fine_hexes:
        fx, fy = to_xy(fh["center_lat"], fh["center_lon"])
        best_idx = -1
        best_d2 = float("inf")
        for idx, ch in enumerate(coarse):
            dx = ch["center_x"] - fx
            dy = ch["center_y"] - fy
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
                best_idx = idx
        if best_idx >= 0:
            groups[best_idx].append(fh)
    return groups


def _mode(values: list[str], default: str) -> str:
    if not values:
        return default
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda k: counts[k])


# ---------------------------------------------------------------------------
# Main seeding logic
# ---------------------------------------------------------------------------

def seed_segments(mission_id: int, hex_data: list[dict]) -> list[int]:
    """Generate hex segment grid, aggregate hex terrain stats, compute POA,
    bulk-insert. Returns list of inserted segment ids in row-major order."""
    mission = get_mission(mission_id)
    if mission is None:
        raise ValueError(f"Mission {mission_id} not found")

    area_geojson = mission["area_geojson"]
    if area_geojson is None:
        raise ValueError(f"Mission {mission_id} has no area_geom")

    pls_lat: float = mission["pls_lat"]
    pls_lon: float = mission["pls_lon"]

    min_lat, min_lon, max_lat, max_lon = _bbox_from_geojson(area_geojson)
    mid_lat = (min_lat + max_lat) / 2.0
    mid_lon = (min_lon + max_lon) / 2.0

    to_xy, to_latlon = _make_projection(mid_lat, mid_lon)

    log.info(
        "Mission %d bbox lat=[%.5f,%.5f] lon=[%.5f,%.5f]",
        mission_id, min_lat, max_lat, min_lon, max_lon,
    )

    # Idempotent: drop existing segments before re-seeding.
    with session() as conn:
        conn.execute("DELETE FROM segments WHERE mission_id = ?", (mission_id,))

    coarse = _generate_hex_grid(min_lat, min_lon, max_lat, max_lon, to_xy, to_latlon)
    log.info("Generated %d coarse hex segments", len(coarse))

    # PLS elevation from the nearest fine hex (planar distance is fine here).
    pls_elev_m = 100.0
    if hex_data:
        best_d2 = float("inf")
        for h in hex_data:
            dx = h["center_lon"] - pls_lon
            dy = h["center_lat"] - pls_lat
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
                pls_elev_m = h.get("center_elev_m", 100.0)

    # Fine-hex → coarse-hex aggregation (nearest center == contains for
    # regular flat-top grid).
    groups = _assign_fine_to_coarse(hex_data, coarse, to_xy)

    rows: list[dict] = []
    centers: list[tuple[float, float]] = []
    elevs: list[float] = []
    covers: list[str] = []
    has_trails: list[bool] = []
    aggs: list[dict] = []  # parallel array for downstream POA + INSERT

    for idx, ch in enumerate(coarse):
        contained = groups.get(idx, [])
        if contained:
            avg_slope = sum(h.get("slope_deg", 0.0) for h in contained) / len(contained)
            dominant_cover = _mode([h.get("dominant_cover", "mixed") for h in contained], "mixed")
            avg_elev = sum(h.get("center_elev_m", 100.0) for h in contained) / len(contained)
            trail_hex_count = sum(1 for h in contained if h.get("has_trail", False))
            trail_length_m = trail_hex_count * 30.0
            has_trail = trail_hex_count > 0
        else:
            # Coarse hex sticking out beyond the fetched terrain — give it
            # defaults so the row still inserts and the agent can see it.
            avg_slope = 0.0
            dominant_cover = "mixed"
            avg_elev = pls_elev_m
            trail_length_m = 0.0
            has_trail = False

        centers.append((ch["center_lat"], ch["center_lon"]))
        elevs.append(avg_elev)
        covers.append(dominant_cover)
        has_trails.append(has_trail)
        aggs.append({
            "row": ch["row"],
            "col": ch["col"],
            "poly_geojson": ch["poly_geojson"],
            "avg_slope_deg": avg_slope,
            "dominant_cover": dominant_cover,
            "trail_length_m": trail_length_m,
        })

    if not aggs:
        log.warning("No segments generated — check mission area_geom")
        return []

    raw_weights = initial_poa_weights(
        cell_centers=centers,
        cell_elev_m=elevs,
        cell_cover=covers,
        cell_has_trail=has_trails,
        pls_lat=pls_lat,
        pls_lon=pls_lon,
        pls_elev_m=pls_elev_m,
    )
    total_w = sum(raw_weights) or 1.0

    for i, ag in enumerate(aggs):
        rows.append({
            "name": f"S-r{ag['row']:02d}-c{ag['col']:02d}",
            "poly_geojson": ag["poly_geojson"],
            "area_m2": round(HEX_AREA_M2, 1),
            "poa": round(raw_weights[i] / total_w, 6),
            "avg_slope_deg": round(ag["avg_slope_deg"], 2),
            "dominant_cover": ag["dominant_cover"],
            "trail_length_m": round(ag["trail_length_m"], 1),
        })

    segment_ids = bulk_insert_segments(mission_id, rows)
    log.info("Inserted %d hex segments for mission %d", len(segment_ids), mission_id)
    return segment_ids


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

    print(f"[seed_segments] mission_id={args.mission_id}", flush=True)
    print("[seed_segments] CLI mode: running with mock terrain data", flush=True)
    try:
        import os
        os.environ.setdefault("TERRAIN_MOCK", "1")
        from scripts.fetch_terrain import fetch_terrain
        result = fetch_terrain(args.mission_id, mock=True)
        hex_data = result["hex_data"]
        ids = seed_segments(args.mission_id, hex_data)
    except Exception as exc:
        print(f"[seed_segments] ERROR: {exc}", file=sys.stderr, flush=True)
        return 1

    print(f"[seed_segments] done: {len(ids)} segments inserted", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
