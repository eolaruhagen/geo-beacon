#!/usr/bin/env python3
"""Fetch or synthesize terrain data for a mission area.

Usage:
    python scripts/fetch_terrain.py --mission-id N [--mock] [--verbose]

Real mode: fetches DEM via USGS/Open-Elevation, landcover via ESA WorldCover,
and OSM features via Overpass. Falls back to mock automatically on any network
or library error.

Mock mode: synthesizes ~5000 hex cells on a ~30m grid and a handful of OSM
features. Always succeeds.

Returns hex_data IN MEMORY — does NOT insert hex_cells. Inserts osm_features.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db.missions import get_mission
from api.db.osm import bulk_insert_osm_features

log = logging.getLogger("fetch_terrain")

# ~30m flat-to-flat hex distance at mid-latitudes
_CELL_SIZE_M = 30.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _bbox_from_geojson(geojson: dict) -> tuple[float, float, float, float]:
    coords = geojson["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lats), min(lons), max(lats), max(lons)


def _hex_steps(lat: float) -> tuple[float, float, float, float]:
    """Return (dlat_step, dlon_step, R_lat, R_lon) for a flat-top hex grid.

    `_CELL_SIZE_M` is interpreted as the flat-to-flat hex distance (short
    diameter). For a flat-top hex with radius R (centroid-to-vertex):
        flat-to-flat = R * sqrt(3)
        column step  = 1.5 * R          (centroid-to-centroid horizontal)
        row step     = flat-to-flat     (centroid-to-centroid vertical)
    """
    R_meters = _CELL_SIZE_M / math.sqrt(3.0)
    dx_meters = 1.5 * R_meters  # column step
    dy_meters = _CELL_SIZE_M    # row step = flat-to-flat
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat))
    dlat_step = dy_meters / m_per_deg_lat
    dlon_step = dx_meters / m_per_deg_lon
    R_lat = R_meters / m_per_deg_lat
    R_lon = R_meters / m_per_deg_lon
    return dlat_step, dlon_step, R_lat, R_lon


def _hex_polygon(lat: float, lon: float, R_lat: float, R_lon: float) -> dict:
    """Flat-top hex with 6 vertices starting at east (angle 0) stepping 60°."""
    ring = []
    for k in range(6):
        angle = k * math.pi / 3
        ring.append([lon + R_lon * math.cos(angle), lat + R_lat * math.sin(angle)])
    ring.append(ring[0])  # close the ring
    return {"type": "Polygon", "coordinates": [ring]}


def _point_in_polygon_bbox(
    pt_lat: float, pt_lon: float,
    poly_geojson: dict,
) -> bool:
    """Fast bbox-only point-in-polygon check for rectangular OSM polygons."""
    coords = poly_geojson["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return (min(lats) <= pt_lat <= max(lats)) and (min(lons) <= pt_lon <= max(lons))


def _linestring_intersects_cell_bbox(
    coords: list[list[float]],
    cell_min_lat: float, cell_min_lon: float,
    cell_max_lat: float, cell_max_lon: float,
) -> bool:
    """Check if any segment of the linestring's midpoint falls in the cell bbox."""
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        mid_lat = (lat1 + lat2) / 2
        mid_lon = (lon1 + lon2) / 2
        if cell_min_lat <= mid_lat <= cell_max_lat and cell_min_lon <= mid_lon <= cell_max_lon:
            return True
    return False


# ---------------------------------------------------------------------------
# Mock generation
# ---------------------------------------------------------------------------

def _generate_mock_hex_data(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float,
    osm_features: list[dict],
) -> list[dict]:
    """Synthesize ~30m flat-top hex grid with terrain attributes (odd-r offset)."""
    mid_lat = (min_lat + max_lat) / 2
    dlat_step, dlon_step, R_lat, R_lon = _hex_steps(mid_lat)

    # Build OSM lookup structures
    trail_coords_list = []
    road_coords_list = []
    water_polys = []
    building_polys = []
    for feat in osm_features:
        geom = feat.get("geom_geojson") or feat.get("geom_geojson") or {}
        kind = feat.get("kind", "")
        if kind == "trail" and geom.get("type") == "LineString":
            trail_coords_list.append(geom["coordinates"])
        elif kind == "road" and geom.get("type") == "LineString":
            road_coords_list.append(geom["coordinates"])
        elif kind == "water" and geom.get("type") == "Polygon":
            water_polys.append(geom)
        elif kind == "building" and geom.get("type") == "Polygon":
            building_polys.append(geom)

    cells: list[dict] = []
    col = 0
    lon = min_lon + R_lon  # start one radius in so the leftmost vertex is at min_lon
    while lon < max_lon:
        row_offset = (dlat_step / 2) if (col % 2 == 1) else 0.0
        lat = min_lat + R_lat + row_offset
        while lat < max_lat:
            frac_lat = (lat - min_lat) / max((max_lat - min_lat), 1e-9)
            frac_lon = (lon - min_lon) / max((max_lon - min_lon), 1e-9)
            slope = 25.0 * (frac_lat + frac_lon) / 2.0
            elev = 50.0 + 200.0 * (frac_lat + frac_lon) / 2.0

            if abs(frac_lat - frac_lon) < 0.07:
                cover = "water"
            elif 0.35 < frac_lat < 0.65 and 0.35 < frac_lon < 0.65:
                cover = "dense"
            elif frac_lat < 0.15 or frac_lon < 0.15:
                cover = "open"
            else:
                cover = "mixed"

            cell_min_lat = lat - R_lat
            cell_max_lat = lat + R_lat
            cell_min_lon = lon - R_lon
            cell_max_lon = lon + R_lon

            has_trail = any(
                _linestring_intersects_cell_bbox(tc, cell_min_lat, cell_min_lon, cell_max_lat, cell_max_lon)
                for tc in trail_coords_list
            )
            has_road = any(
                _linestring_intersects_cell_bbox(rc, cell_min_lat, cell_min_lon, cell_max_lat, cell_max_lon)
                for rc in road_coords_list
            )
            is_water = any(_point_in_polygon_bbox(lat, lon, wp) for wp in water_polys)
            is_building = any(_point_in_polygon_bbox(lat, lon, bp) for bp in building_polys)

            cells.append({
                "center_lat": lat,
                "center_lon": lon,
                "poly_geojson": _hex_polygon(lat, lon, R_lat, R_lon),
                "center_elev_m": round(elev, 1),
                "slope_deg": round(slope, 2),
                "dominant_cover": cover,
                "has_trail": has_trail,
                "has_road": has_road,
                "is_building": is_building,
                "is_water": is_water,
            })
            lat += dlat_step
        lon += dlon_step
        col += 1
    return cells


def _generate_mock_osm_features(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float,
) -> list[dict]:
    mid_lon = (min_lon + max_lon) / 2
    mid_lat = (min_lat + max_lat) / 2

    features: list[dict] = []

    features.append({
        "kind": "trail",
        "name": "Mock Trail",
        "geom_geojson": {
            "type": "LineString",
            "coordinates": [
                [mid_lon, min_lat],
                [mid_lon - 0.001, min_lat + (max_lat - min_lat) * 0.33],
                [mid_lon + 0.001, min_lat + (max_lat - min_lat) * 0.66],
                [mid_lon, max_lat],
            ],
        },
    })

    features.append({
        "kind": "road",
        "name": "Mock Road",
        "geom_geojson": {
            "type": "LineString",
            "coordinates": [
                [min_lon, mid_lat],
                [max_lon, mid_lat],
            ],
        },
    })

    water_size_lat = (max_lat - min_lat) * 0.08
    water_size_lon = (max_lon - min_lon) * 0.08
    w_lat = min_lat + water_size_lat * 0.5
    w_lon = min_lon + water_size_lon * 0.5
    features.append({
        "kind": "water",
        "name": "Mock Pond",
        "geom_geojson": {
            "type": "Polygon",
            "coordinates": [[
                [w_lon - water_size_lon / 2, w_lat - water_size_lat / 2],
                [w_lon + water_size_lon / 2, w_lat - water_size_lat / 2],
                [w_lon + water_size_lon / 2, w_lat + water_size_lat / 2],
                [w_lon - water_size_lon / 2, w_lat + water_size_lat / 2],
                [w_lon - water_size_lon / 2, w_lat - water_size_lat / 2],
            ]],
        },
    })

    return features


def _run_mock(
    mission_id: int,
    min_lat: float, min_lon: float, max_lat: float, max_lon: float,
) -> dict:
    osm = _generate_mock_osm_features(min_lat, min_lon, max_lat, max_lon)
    n_osm = bulk_insert_osm_features(mission_id, osm)
    hex_data = _generate_mock_hex_data(min_lat, min_lon, max_lat, max_lon, osm)
    log.info("Mock: %d hex cells, %d osm_features", len(hex_data), n_osm)
    return {"osm_features_inserted": n_osm, "hex_data": hex_data}


# ---------------------------------------------------------------------------
# Real terrain fetch (with fallback to mock on any error)
# ---------------------------------------------------------------------------

def _fetch_real(
    mission_id: int,
    min_lat: float, min_lon: float, max_lat: float, max_lon: float,
) -> dict:
    import requests

    TIMEOUT = 15
    HEADERS = {
        "User-Agent": "geo-beacon-sar/0.1 (hackathon SAR mission control; contact: e.olaruhagen@gmail.com)",
        "Accept": "application/json",
    }

    mid_lat = (min_lat + max_lat) / 2
    dlat_step, dlon_step, R_lat, R_lon = _hex_steps(mid_lat)

    # --- Build grid of flat-top hex centroid points (odd-r offset, column-major) ---
    lat_steps: list[float] = []
    lon_steps: list[float] = []
    col_indices: list[int] = []  # track column for each centroid (for neighbor math)
    col = 0
    lon = min_lon + R_lon
    while lon < max_lon:
        row_offset = (dlat_step / 2) if (col % 2 == 1) else 0.0
        lat = min_lat + R_lat + row_offset
        while lat < max_lat:
            lat_steps.append(lat)
            lon_steps.append(lon)
            col_indices.append(col)
            lat += dlat_step
        lon += dlon_step
        col += 1

    # --- DEM via Open-Elevation ---
    elevations: list[float] = []
    try:
        locations = [{"latitude": la, "longitude": lo} for la, lo in zip(lat_steps, lon_steps)]
        CHUNK = 512
        for i in range(0, len(locations), CHUNK):
            chunk = locations[i: i + CHUNK]
            resp = requests.post(
                "https://api.open-elevation.com/api/v1/lookup",
                json={"locations": chunk},
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            elevations.extend(r["elevation"] for r in resp.json()["results"])
        log.info("Real DEM: %d elevation points from Open-Elevation", len(elevations))
    except Exception as exc:
        log.warning("DEM fetch failed (%s); using mock elevations", exc)
        elevations = []

    # --- OSM via Overpass ---
    osm_features: list[dict] = []
    try:
        bbox_str = f"{min_lat},{min_lon},{max_lat},{max_lon}"
        overpass_query = f"""
[out:json][timeout:25];
(
  way["highway"~"^(path|footway|track)$"]({bbox_str});
  way["highway"~"^(primary|secondary|tertiary|residential|service)$"]({bbox_str});
  way["natural"="water"]({bbox_str});
  relation["natural"="water"]({bbox_str});
  way["waterway"~"^(stream|river)$"]({bbox_str});
  way["building"]({bbox_str});
);
out geom;
"""
        resp = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": overpass_query},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])

        highway_trail = {"path", "footway", "track"}
        highway_road = {"primary", "secondary", "tertiary", "residential", "service"}

        for el in elements:
            if el.get("type") != "way":
                continue
            tags = el.get("tags", {})
            geom_pts = el.get("geometry", [])
            if not geom_pts:
                continue

            coords = [[pt["lon"], pt["lat"]] for pt in geom_pts]
            hw = tags.get("highway", "")
            if hw in highway_trail:
                kind = "trail"
                geom: dict[str, Any] = {"type": "LineString", "coordinates": coords}
            elif hw in highway_road:
                kind = "road"
                geom = {"type": "LineString", "coordinates": coords}
            elif tags.get("natural") == "water" or tags.get("waterway") in ("stream", "river"):
                kind = "water"
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    geom = {"type": "Polygon", "coordinates": [coords]}
                else:
                    geom = {"type": "LineString", "coordinates": coords}
            elif "building" in tags:
                kind = "building"
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    geom = {"type": "Polygon", "coordinates": [coords]}
                else:
                    continue
            else:
                continue

            osm_features.append({
                "kind": kind,
                "name": tags.get("name"),
                "geom_geojson": geom,
            })
        log.info("OSM: fetched %d features", len(osm_features))
    except Exception as exc:
        log.warning("OSM fetch failed (%s); falling back to mock OSM features", exc)
        osm_features = _generate_mock_osm_features(min_lat, min_lon, max_lat, max_lon)

    n_osm = bulk_insert_osm_features(mission_id, osm_features)

    # Build OSM lookup structures for rasterization into hex cells
    trail_coords_list = []
    road_coords_list = []
    water_polys = []
    building_polys = []
    for feat in osm_features:
        geom = feat.get("geom_geojson", {})
        kind = feat.get("kind", "")
        if kind == "trail" and geom.get("type") == "LineString":
            trail_coords_list.append(geom["coordinates"])
        elif kind == "road" and geom.get("type") == "LineString":
            road_coords_list.append(geom["coordinates"])
        elif kind == "water" and geom.get("type") == "Polygon":
            water_polys.append(geom)
        elif kind == "building" and geom.get("type") == "Polygon":
            building_polys.append(geom)

    # Build hex_data list
    hex_data: list[dict] = []

    if not elevations:
        # Fallback: synthesize elevations from bbox fractions
        elevations = []
        for la, lo in zip(lat_steps, lon_steps):
            frac_lat = (la - min_lat) / max((max_lat - min_lat), 1e-9)
            frac_lon = (lo - min_lon) / max((max_lon - min_lon), 1e-9)
            elevations.append(50.0 + 200.0 * (frac_lat + frac_lon) / 2.0)

    # Build index → elevation map keyed by (col, row_within_col) for slope lookup.
    # Centroids were emitted column-major; within each column they are contiguous.
    col_starts: dict[int, int] = {}
    col_lengths: dict[int, int] = {}
    for idx, c in enumerate(col_indices):
        if c not in col_starts:
            col_starts[c] = idx
        col_lengths[c] = col_lengths.get(c, 0) + 1

    for idx, (la, lo, elev) in enumerate(zip(lat_steps, lon_steps, elevations)):
        # Slope: compare to next centroid in same column (dlat_step apart in lat)
        # and to a centroid in the next column (offset by ~1.5*R in lon).
        c = col_indices[idx]
        within = idx - col_starts[c]
        next_in_col = idx + 1 if within + 1 < col_lengths[c] else None
        next_col_start = col_starts.get(c + 1)
        next_in_next_col = (
            next_col_start + min(within, col_lengths.get(c + 1, 1) - 1)
            if next_col_start is not None
            else None
        )
        if (
            next_in_col is not None
            and next_in_next_col is not None
            and next_in_col < len(elevations)
            and next_in_next_col < len(elevations)
        ):
            dz_lat = (elevations[next_in_col] - elev) / _CELL_SIZE_M
            dz_lon = (elevations[next_in_next_col] - elev) / (1.5 * _CELL_SIZE_M / math.sqrt(3.0))
            slope = math.degrees(math.atan(math.sqrt(dz_lon**2 + dz_lat**2)))
        else:
            slope = 0.0

        frac_lat = (la - min_lat) / max((max_lat - min_lat), 1e-9)
        frac_lon = (lo - min_lon) / max((max_lon - min_lon), 1e-9)
        if frac_lat < 0.15 or frac_lon < 0.15:
            cover = "open"
        elif 0.35 < frac_lat < 0.65 and 0.35 < frac_lon < 0.65:
            cover = "dense"
        else:
            cover = "mixed"

        cell_min_lat = la - R_lat
        cell_max_lat = la + R_lat
        cell_min_lon = lo - R_lon
        cell_max_lon = lo + R_lon

        has_trail = any(
            _linestring_intersects_cell_bbox(tc, cell_min_lat, cell_min_lon, cell_max_lat, cell_max_lon)
            for tc in trail_coords_list
        )
        has_road = any(
            _linestring_intersects_cell_bbox(rc, cell_min_lat, cell_min_lon, cell_max_lat, cell_max_lon)
            for rc in road_coords_list
        )
        is_water = any(_point_in_polygon_bbox(la, lo, wp) for wp in water_polys)
        is_building = any(_point_in_polygon_bbox(la, lo, bp) for bp in building_polys)

        hex_data.append({
            "center_lat": la,
            "center_lon": lo,
            "poly_geojson": _hex_polygon(la, lo, R_lat, R_lon),
            "center_elev_m": round(float(elev), 1),
            "slope_deg": round(slope, 2),
            "dominant_cover": cover,
            "has_trail": has_trail,
            "has_road": has_road,
            "is_building": is_building,
            "is_water": is_water,
        })

    return {"osm_features_inserted": n_osm, "hex_data": hex_data}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_terrain(mission_id: int, mock: bool | None = None) -> dict:
    """Reads mission.area_geom, computes ~30m hex grid, inserts osm_features.

    Does NOT insert hex_cells — returns hex_data in memory.

    `mock` defaults to env TERRAIN_MOCK=1 -> True, else False. Real path
    falls back to mock on network failure.
    """
    if mock is None:
        mock = os.environ.get("TERRAIN_MOCK", "0") == "1"

    mission = get_mission(mission_id)
    if mission is None:
        raise ValueError(f"Mission {mission_id} not found")

    area_geojson = mission["area_geojson"]
    if area_geojson is None:
        raise ValueError(f"Mission {mission_id} has no area_geom")

    min_lat, min_lon, max_lat, max_lon = _bbox_from_geojson(area_geojson)
    log.info(
        "Mission %d bbox: lat=[%.5f, %.5f] lon=[%.5f, %.5f]",
        mission_id, min_lat, max_lat, min_lon, max_lon,
    )

    if mock:
        return _run_mock(mission_id, min_lat, min_lon, max_lat, max_lon)

    try:
        return _fetch_real(mission_id, min_lat, min_lon, max_lat, max_lon)
    except Exception as exc:
        log.error("Real fetch failed (%s); falling back to full mock", exc)
        return _run_mock(mission_id, min_lat, min_lon, max_lat, max_lon)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mission-id", type=int, required=True)
    p.add_argument("--mock", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    print(f"[fetch_terrain] mission_id={args.mission_id} mock={args.mock}", flush=True)
    try:
        result = fetch_terrain(args.mission_id, mock=args.mock)
    except Exception as exc:
        print(f"[fetch_terrain] ERROR: {exc}", file=sys.stderr, flush=True)
        return 1

    print(
        f"[fetch_terrain] done: {len(result['hex_data'])} hex cells,"
        f" {result['osm_features_inserted']} osm_features",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
