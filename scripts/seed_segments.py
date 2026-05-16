#!/usr/bin/env python3
"""Subdivide a mission area into ~100m grid segments and compute initial POA.

Usage:
    python scripts/seed_segments.py --mission-id N [--verbose]

Reads mission from DB, subdivides bbox into ~100m segments, aggregates terrain
stats from hex_data passed in memory, computes POA, bulk-inserts segments.

New signature: seed_segments(mission_id, hex_data) -> list[int]
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import session
from api.db.missions import get_mission
from api.db.segments import bulk_insert_segments
from agent.poa import initial_poa_weights

log = logging.getLogger("seed_segments")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _bbox_from_geojson(geojson: dict) -> tuple[float, float, float, float]:
    coords = geojson["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lats), min(lons), max(lats), max(lons)


def _deg_per_100m(lat: float) -> tuple[float, float]:
    dlat = 100.0 / 111_320.0
    dlon = 100.0 / (111_320.0 * math.cos(math.radians(lat)))
    return dlat, dlon


def _cell_polygon_geojson(lat: float, lon: float, dlat: float, dlon: float) -> dict:
    half_lat = dlat / 2
    half_lon = dlon / 2
    ring = [
        [lon - half_lon, lat - half_lat],
        [lon + half_lon, lat - half_lat],
        [lon + half_lon, lat + half_lat],
        [lon - half_lon, lat + half_lat],
        [lon - half_lon, lat - half_lat],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _approx_area_m2(dlat: float, dlon: float, lat: float) -> float:
    h = dlat * 111_320.0
    w = dlon * 111_320.0 * math.cos(math.radians(lat))
    return h * w


# ---------------------------------------------------------------------------
# Hex data aggregation helpers
# ---------------------------------------------------------------------------

def _build_hex_index(
    hex_data: list[dict],
) -> list[tuple[float, float, float, float, dict]]:
    """Return (min_lat, min_lon, max_lat, max_lon, hex) for fast bbox lookup."""
    index = []
    for h in hex_data:
        coords = h["poly_geojson"]["coordinates"][0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        index.append((min(lats), min(lons), max(lats), max(lons), h))
    return index


def _hexes_in_bbox(
    seg_min_lat: float, seg_min_lon: float,
    seg_max_lat: float, seg_max_lon: float,
    hex_index: list[tuple[float, float, float, float, dict]],
) -> list[dict]:
    result = []
    for h_min_lat, h_min_lon, h_max_lat, h_max_lon, h in hex_index:
        if h_max_lat < seg_min_lat or h_min_lat > seg_max_lat:
            continue
        if h_max_lon < seg_min_lon or h_min_lon > seg_max_lon:
            continue
        result.append(h)
    return result


def _mode(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda k: counts[k])


# ---------------------------------------------------------------------------
# Main seeding logic
# ---------------------------------------------------------------------------

def seed_segments(mission_id: int, hex_data: list[dict]) -> list[int]:
    """Subdivide mission area into ~100m segments, aggregate hex terrain stats,
    compute POA, bulk-insert. Returns list of inserted segment ids."""
    mission = get_mission(mission_id)
    if mission is None:
        raise ValueError(f"Mission {mission_id} not found")

    area_geojson = mission["area_geojson"]
    if area_geojson is None:
        raise ValueError(f"Mission {mission_id} has no area_geom")

    pls_lat: float = mission["pls_lat"]
    pls_lon: float = mission["pls_lon"]

    min_lat, min_lon, max_lat, max_lon = _bbox_from_geojson(area_geojson)
    mid_lat = (min_lat + max_lat) / 2
    dlat, dlon = _deg_per_100m(mid_lat)

    log.info(
        "Mission %d bbox lat=[%.5f,%.5f] lon=[%.5f,%.5f]",
        mission_id, min_lat, max_lat, min_lon, max_lon,
    )

    # Idempotent: remove existing segments before re-seeding.
    with session() as conn:
        conn.execute("DELETE FROM segments WHERE mission_id = ?", (mission_id,))

    hex_index = _build_hex_index(hex_data)
    log.info("Loaded %d hex cells for aggregation", len(hex_data))

    # Find PLS elevation from hex nearest to PLS
    pls_elev_m = 100.0
    if hex_data:
        best_dist = float("inf")
        for h in hex_data:
            d = math.sqrt((h["center_lat"] - pls_lat) ** 2 + (h["center_lon"] - pls_lon) ** 2)
            if d < best_dist:
                best_dist = d
                pls_elev_m = h.get("center_elev_m", 100.0)

    # Per-segment accumulators
    seg_centers: list[tuple[float, float]] = []
    seg_elev_m: list[float] = []
    seg_cover: list[str] = []
    seg_has_trail: list[bool] = []
    seg_slope: list[float] = []
    seg_trail_len_m: list[float] = []
    seg_poly_geojsons: list[dict] = []
    seg_area_m2: list[float] = []

    lat = min_lat + dlat / 2
    while lat < max_lat:
        lon = min_lon + dlon / 2
        while lon < max_lon:
            seg_min_lat = lat - dlat / 2
            seg_max_lat = lat + dlat / 2
            seg_min_lon = lon - dlon / 2
            seg_max_lon = lon + dlon / 2

            contained = _hexes_in_bbox(
                seg_min_lat, seg_min_lon, seg_max_lat, seg_max_lon, hex_index
            )

            if contained:
                avg_slope = sum(h.get("slope_deg", 0.0) for h in contained) / len(contained)
                dominant_cover = _mode([h.get("dominant_cover", "mixed") for h in contained])
                avg_elev = sum(h.get("center_elev_m", 100.0) for h in contained) / len(contained)
                # trail_length_m: count of hexes with has_trail * 30m cell size
                trail_hex_count = sum(1 for h in contained if h.get("has_trail", False))
                trail_length_m = trail_hex_count * 30.0
                has_trail = trail_hex_count > 0
            else:
                avg_slope = 0.0
                dominant_cover = "mixed"
                avg_elev = pls_elev_m
                trail_length_m = 0.0
                has_trail = False

            seg_centers.append((lat, lon))
            seg_elev_m.append(avg_elev)
            seg_cover.append(dominant_cover)
            seg_has_trail.append(has_trail)
            seg_slope.append(avg_slope)
            seg_trail_len_m.append(trail_length_m)
            seg_poly_geojsons.append(_cell_polygon_geojson(lat, lon, dlat, dlon))
            seg_area_m2.append(_approx_area_m2(dlat, dlon, lat))

            lon += dlon
        lat += dlat

    n_segs = len(seg_centers)
    log.info("Grid has %d segments", n_segs)

    if n_segs == 0:
        log.warning("No segments generated — check mission area_geom")
        return []

    raw_weights = initial_poa_weights(
        cell_centers=seg_centers,
        cell_elev_m=seg_elev_m,
        cell_cover=seg_cover,
        cell_has_trail=seg_has_trail,
        pls_lat=pls_lat,
        pls_lon=pls_lon,
        pls_elev_m=pls_elev_m,
    )

    total_w = sum(raw_weights) or 1.0

    rows: list[dict] = []
    for i in range(n_segs):
        rows.append({
            "name": f"S-{i + 1:03d}",
            "poly_geojson": seg_poly_geojsons[i],
            "area_m2": round(seg_area_m2[i], 1),
            "poa": round(raw_weights[i] / total_w, 6),
            "avg_slope_deg": round(seg_slope[i], 2),
            "dominant_cover": seg_cover[i],
            "trail_length_m": round(seg_trail_len_m[i], 1),
        })

    segment_ids = bulk_insert_segments(mission_id, rows)
    log.info("Inserted %d segments for mission %d", len(segment_ids), mission_id)
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
