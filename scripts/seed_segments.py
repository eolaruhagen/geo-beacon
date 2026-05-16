#!/usr/bin/env python3
"""Subdivide a mission area into ~100 m grid segments and compute initial POA.

Usage:
    python scripts/seed_segments.py --mission-id N [--verbose]

Reads the mission area + pre-fetched terrain_cells + osm_features from the DB,
subdivides the bbox into a ~100 m grid, spatial-joins terrain data for each cell,
computes initial POA weights via agent.poa, then bulk-inserts into segments.

Idempotent: deletes existing segments for this mission before inserting.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import session
from api.db.missions import get_mission
from api.db.terrain import osm_features_for_mission, terrain_cells_for_mission
from api.db.segments import bulk_insert_segments
from agent.poa import initial_poa_weights

log = logging.getLogger("seed_segments")


# ---------------------------------------------------------------------------
# Geometry helpers (no shapely required — pure Python for portability)
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
    """Area of a grid cell in m² using flat-earth approximation."""
    h = dlat * 111_320.0
    w = dlon * 111_320.0 * math.cos(math.radians(lat))
    return h * w


def _cell_bbox(lat: float, lon: float, dlat: float, dlon: float) -> tuple[float, float, float, float]:
    return lat - dlat / 2, lon - dlon / 2, lat + dlat / 2, lon + dlon / 2


def _point_in_bbox(
    pt_lat: float, pt_lon: float,
    min_lat: float, min_lon: float, max_lat: float, max_lon: float,
) -> bool:
    return min_lat <= pt_lat <= max_lat and min_lon <= pt_lon <= max_lon


def _linestring_length_m(coords: list[list[float]]) -> float:
    """Rough great-circle length of a LineString in metres."""
    R = 6_371_000.0
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        total += 2 * R * math.asin(math.sqrt(a))
    return total


def _clip_linestring_to_bbox(
    coords: list[list[float]],
    min_lat: float, min_lon: float, max_lat: float, max_lon: float,
) -> list[list[float]]:
    """Return only segments whose midpoint falls inside the bbox (fast approximation)."""
    clipped = []
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        mid_lat = (lat1 + lat2) / 2
        mid_lon = (lon1 + lon2) / 2
        if _point_in_bbox(mid_lat, mid_lon, min_lat, min_lon, max_lat, max_lon):
            if not clipped:
                clipped.append(coords[i])
            clipped.append(coords[i + 1])
    return clipped


# ---------------------------------------------------------------------------
# Terrain cell index (spatial join via bbox overlap)
# ---------------------------------------------------------------------------

def _build_cell_index(
    terrain_cells: list[dict],
) -> list[tuple[float, float, float, float, dict]]:
    """Return list of (min_lat, min_lon, max_lat, max_lon, cell) for fast lookup."""
    index = []
    for cell in terrain_cells:
        geojson = cell.get("geom_geojson") or cell.get("poly_geojson")
        if geojson is None:
            continue
        coords = geojson["coordinates"][0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        index.append((min(lats), min(lons), max(lats), max(lons), cell))
    return index


def _find_overlapping_cells(
    seg_min_lat: float, seg_min_lon: float, seg_max_lat: float, seg_max_lon: float,
    cell_index: list[tuple[float, float, float, float, dict]],
) -> list[dict]:
    result = []
    for c_min_lat, c_min_lon, c_max_lat, c_max_lon, cell in cell_index:
        if c_max_lat < seg_min_lat or c_min_lat > seg_max_lat:
            continue
        if c_max_lon < seg_min_lon or c_min_lon > seg_max_lon:
            continue
        result.append(cell)
    return result


# ---------------------------------------------------------------------------
# Main seeding logic
# ---------------------------------------------------------------------------

def seed_segments(mission_id: int) -> int:
    """Read mission + terrain from DB, subdivide, compute POA, bulk-insert segments.

    Returns number of segments inserted.
    """
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
        "Mission %d bbox lat=[%.5f,%.5f] lon=[%.5f,%.5f] dlat=%.6f dlon=%.6f",
        mission_id, min_lat, max_lat, min_lon, max_lon, dlat, dlon,
    )

    # Idempotent: remove existing segments before re-seeding.
    with session() as conn:
        conn.execute("DELETE FROM segments WHERE mission_id = ?", (mission_id,))

    terrain_cells = terrain_cells_for_mission(mission_id)
    osm_features = osm_features_for_mission(mission_id)
    log.info("Loaded %d terrain_cells, %d osm_features", len(terrain_cells), len(osm_features))

    cell_index = _build_cell_index(terrain_cells)

    # Separate trail linestrings for trail_length_m computation.
    trail_coords_list: list[list[list[float]]] = []
    for feat in osm_features:
        if feat.get("kind") != "trail":
            continue
        geojson = feat.get("geom_geojson") or feat.get("geom")
        if geojson is None:
            continue
        if geojson["type"] == "LineString":
            trail_coords_list.append(geojson["coordinates"])

    # Collect PLS elevation from terrain cells (find nearest cell to PLS).
    pls_elev_m = 100.0  # fallback
    if terrain_cells:
        best_dist = float("inf")
        for cell in terrain_cells:
            geojson = cell.get("geom_geojson") or cell.get("poly_geojson")
            if geojson is None:
                continue
            coords = geojson["coordinates"][0]
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            c_lat = sum(lats) / len(lats)
            c_lon = sum(lons) / len(lons)
            d = math.sqrt((c_lat - pls_lat) ** 2 + (c_lon - pls_lon) ** 2)
            if d < best_dist:
                best_dist = d
                pls_elev_m = cell.get("center_elev_m", 100.0)

    # Grid iteration.
    cell_centers: list[tuple[float, float]] = []
    cell_elev_m: list[float] = []
    cell_cover: list[str] = []
    cell_has_trail: list[bool] = []
    cell_slope: list[float] = []
    cell_trail_len: list[float] = []
    cell_poly_geojsons: list[dict] = []
    cell_area_m2: list[float] = []

    seg_idx = 0
    lat = min_lat + dlat / 2
    while lat < max_lat:
        lon = min_lon + dlon / 2
        while lon < max_lon:
            seg_min_lat, seg_min_lon, seg_max_lat, seg_max_lon = _cell_bbox(lat, lon, dlat, dlon)

            overlapping = _find_overlapping_cells(
                seg_min_lat, seg_min_lon, seg_max_lat, seg_max_lon, cell_index
            )

            # avg_slope_deg + dominant_cover from overlapping terrain cells.
            if overlapping:
                avg_slope = sum(c.get("avg_slope_deg", 0.0) for c in overlapping) / len(overlapping)
                cover_counts: dict[str, int] = {}
                for c in overlapping:
                    cov = c.get("dominant_cover", "mixed")
                    cover_counts[cov] = cover_counts.get(cov, 0) + 1
                dominant_cover = max(cover_counts, key=lambda k: cover_counts[k])
                avg_elev = sum(c.get("center_elev_m", 100.0) for c in overlapping) / len(overlapping)
            else:
                avg_slope = 0.0
                dominant_cover = "mixed"
                avg_elev = pls_elev_m

            # trail_length_m: sum clipped trail segments that pass through this cell.
            trail_len = 0.0
            for trail_coords in trail_coords_list:
                clipped = _clip_linestring_to_bbox(
                    trail_coords, seg_min_lat, seg_min_lon, seg_max_lat, seg_max_lon
                )
                if clipped:
                    trail_len += _linestring_length_m(clipped)

            has_trail = trail_len > 0.0

            cell_centers.append((lat, lon))
            cell_elev_m.append(avg_elev)
            cell_cover.append(dominant_cover)
            cell_has_trail.append(has_trail)
            cell_slope.append(avg_slope)
            cell_trail_len.append(trail_len)
            cell_poly_geojsons.append(_cell_polygon_geojson(lat, lon, dlat, dlon))
            cell_area_m2.append(_approx_area_m2(dlat, dlon, lat))

            seg_idx += 1
            lon += dlon
        lat += dlat

    n_segs = len(cell_centers)
    log.info("Grid has %d segments", n_segs)

    if n_segs == 0:
        log.warning("No segments generated — check mission area_geom")
        return 0

    # Compute raw weights via agent.poa.
    raw_weights = initial_poa_weights(
        cell_centers=cell_centers,
        cell_elev_m=cell_elev_m,
        cell_cover=cell_cover,
        cell_has_trail=cell_has_trail,
        pls_lat=pls_lat,
        pls_lon=pls_lon,
        pls_elev_m=pls_elev_m,
    )

    total_w = sum(raw_weights)
    if total_w <= 0:
        total_w = 1.0

    rows: list[dict] = []
    for i in range(n_segs):
        poa = raw_weights[i] / total_w
        rows.append(
            {
                "name": f"S-{i + 1:03d}",
                "poly_geojson": cell_poly_geojsons[i],
                "area_m2": round(cell_area_m2[i], 1),
                "poa": round(poa, 6),
                "avg_slope_deg": round(cell_slope[i], 2),
                "dominant_cover": cell_cover[i],
                "trail_length_m": round(cell_trail_len[i], 1),
            }
        )

    inserted = bulk_insert_segments(mission_id, rows)
    log.info("Inserted %d segments for mission %d", inserted, mission_id)
    return inserted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mission-id", type=int, required=True, help="DB mission ID")
    p.add_argument("--verbose", action="store_true", help="Debug logging")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    print(f"[seed_segments] mission_id={args.mission_id}", flush=True)
    try:
        n = seed_segments(args.mission_id)
    except Exception as exc:
        print(f"[seed_segments] ERROR: {exc}", file=sys.stderr, flush=True)
        return 1

    print(f"[seed_segments] done: {n} segments inserted", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
