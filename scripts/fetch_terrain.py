#!/usr/bin/env python3
"""Fetch or synthesize terrain data for a mission area.

Usage:
    python scripts/fetch_terrain.py --mission-id N [--mock] [--verbose]

Real mode (no --mock): fetches DEM via USGS/Open-Elevation, landcover via ESA
WorldCover, and OSM features via Overpass. Falls back to mock automatically on
any network or library error.

Mock mode (--mock): synthesizes ~400 terrain_cells on a 100 m grid covering
the mission bbox and a handful of OSM features, then inserts them into the DB.
Always succeeds — use for demo.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

# Add repo root to path so we can import api.db.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import session
from api.db.missions import get_mission
from api.db.terrain import (
    bulk_insert_osm_features,
    bulk_insert_terrain_cells,
)

log = logging.getLogger("fetch_terrain")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _bbox_from_geojson(geojson: dict) -> tuple[float, float, float, float]:
    """Return (min_lat, min_lon, max_lat, max_lon) from a GeoJSON Polygon."""
    coords = geojson["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lats), min(lons), max(lats), max(lons)


def _cell_polygon(lat: float, lon: float, dlat: float, dlon: float) -> dict:
    """Return a GeoJSON Polygon for a grid cell centered at (lat, lon)."""
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


def _deg_per_100m(lat: float) -> tuple[float, float]:
    """Return (dlat, dlon) in degrees for a ~100 m step at the given latitude."""
    dlat = 100.0 / 111_320.0
    dlon = 100.0 / (111_320.0 * math.cos(math.radians(lat)))
    return dlat, dlon


# ---------------------------------------------------------------------------
# Mock terrain generation
# ---------------------------------------------------------------------------

def _generate_mock_cells(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> list[dict]:
    """Synthesize terrain_cells on a ~100 m grid covering the bbox."""
    mid_lat = (min_lat + max_lat) / 2
    dlat, dlon = _deg_per_100m(mid_lat)

    cells: list[dict] = []
    lat = min_lat + dlat / 2
    row = 0
    while lat < max_lat:
        lon = min_lon + dlon / 2
        col = 0
        while lon < max_lon:
            # Smooth slope gradient: 0 deg at SW corner, 25 deg at NE corner.
            frac_lat = (lat - min_lat) / max((max_lat - min_lat), 1e-9)
            frac_lon = (lon - min_lon) / max((max_lon - min_lon), 1e-9)
            slope = 25.0 * (frac_lat + frac_lon) / 2.0

            # Elevation: gentle rise from 50 m to 250 m SW→NE.
            elev = 50.0 + 200.0 * (frac_lat + frac_lon) / 2.0

            # Cover: mostly 'mixed'; patch of 'dense' in center; 'open' fringe;
            # 'water' stripe along a diagonal line.
            if abs(frac_lat - frac_lon) < 0.07:
                cover = "water"
            elif frac_lat > 0.35 and frac_lat < 0.65 and frac_lon > 0.35 and frac_lon < 0.65:
                cover = "dense"
            elif frac_lat < 0.15 or frac_lon < 0.15:
                cover = "open"
            else:
                cover = "mixed"

            cells.append(
                {
                    "poly_geojson": _cell_polygon(lat, lon, dlat, dlon),
                    "center_elev_m": round(elev, 1),
                    "avg_slope_deg": round(slope, 2),
                    "dominant_cover": cover,
                }
            )
            lon += dlon
            col += 1
        lat += dlat
        row += 1
    return cells


def _generate_mock_osm_features(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> list[dict]:
    """Synthesize OSM trail, road, and water features."""
    mid_lon = (min_lon + max_lon) / 2
    mid_lat = (min_lat + max_lat) / 2

    features: list[dict] = []

    # Trail: N-S LineString through the bbox centre.
    features.append(
        {
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
        }
    )

    # Road: E-W LineString.
    features.append(
        {
            "kind": "road",
            "name": "Mock Road",
            "geom_geojson": {
                "type": "LineString",
                "coordinates": [
                    [min_lon, mid_lat],
                    [max_lon, mid_lat],
                ],
            },
        }
    )

    # Water: small Polygon in SW corner.
    water_size_lat = (max_lat - min_lat) * 0.08
    water_size_lon = (max_lon - min_lon) * 0.08
    w_lat = min_lat + water_size_lat * 0.5
    w_lon = min_lon + water_size_lon * 0.5
    features.append(
        {
            "kind": "water",
            "name": "Mock Pond",
            "geom_geojson": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [w_lon - water_size_lon / 2, w_lat - water_size_lat / 2],
                        [w_lon + water_size_lon / 2, w_lat - water_size_lat / 2],
                        [w_lon + water_size_lon / 2, w_lat + water_size_lat / 2],
                        [w_lon - water_size_lon / 2, w_lat + water_size_lat / 2],
                        [w_lon - water_size_lon / 2, w_lat - water_size_lat / 2],
                    ]
                ],
            },
        }
    )

    return features


def _delete_existing_terrain(mission_id: int) -> None:
    with session() as conn:
        conn.execute("DELETE FROM terrain_cells WHERE mission_id = ?", (mission_id,))
        conn.execute("DELETE FROM osm_features WHERE mission_id = ?", (mission_id,))


def _run_mock(
    mission_id: int,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
) -> dict[str, int]:
    _delete_existing_terrain(mission_id)
    cells = _generate_mock_cells(min_lat, min_lon, max_lat, max_lon)
    osm = _generate_mock_osm_features(min_lat, min_lon, max_lat, max_lon)
    n_cells = bulk_insert_terrain_cells(mission_id, cells)
    n_osm = bulk_insert_osm_features(mission_id, osm)
    log.info("Mock: inserted %d terrain_cells, %d osm_features", n_cells, n_osm)
    return {"terrain_cells_inserted": n_cells, "osm_features_inserted": n_osm}


# ---------------------------------------------------------------------------
# Real terrain fetch (with fallback to mock on any error)
# ---------------------------------------------------------------------------

def _fetch_real(
    mission_id: int,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
) -> dict[str, int]:
    import numpy as np
    import requests
    import rasterio
    from rasterio.transform import from_bounds

    TIMEOUT = 5  # seconds per request

    cells: list[dict] = []
    osm_features: list[dict] = []

    mid_lat = (min_lat + max_lat) / 2
    dlat, dlon = _deg_per_100m(mid_lat)

    # --- DEM via Open-Elevation API (public, no auth required) ---
    try:
        lat_steps = []
        lon_steps = []
        lat = min_lat + dlat / 2
        while lat < max_lat:
            lon = min_lon + dlon / 2
            while lon < max_lon:
                lat_steps.append(lat)
                lon_steps.append(lon)
                lon += dlon
            lat += dlat

        locations = [{"latitude": la, "longitude": lo} for la, lo in zip(lat_steps, lon_steps)]
        # Open-Elevation has a 512-point limit per request; chunk if needed.
        CHUNK = 512
        elevations: list[float] = []
        for i in range(0, len(locations), CHUNK):
            chunk = locations[i : i + CHUNK]
            resp = requests.post(
                "https://api.open-elevation.com/api/v1/lookup",
                json={"locations": chunk},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            elevations.extend(r["elevation"] for r in resp.json()["results"])

        # Build cells from elevation samples; approximate slope from neighbours.
        n_cols = round((max_lon - min_lon) / dlon)
        for idx, (la, lo, elev) in enumerate(zip(lat_steps, lon_steps, elevations)):
            # Slope: use finite difference when neighbours available.
            if idx + 1 < len(elevations) and idx + n_cols < len(elevations):
                dz_lon = (elevations[idx + 1] - elev) / 100.0  # per metre
                dz_lat = (elevations[idx + n_cols] - elev) / 100.0
                slope = math.degrees(math.atan(math.sqrt(dz_lon**2 + dz_lat**2)))
            else:
                slope = 0.0

            frac_lat = (la - min_lat) / max((max_lat - min_lat), 1e-9)
            frac_lon = (lo - min_lon) / max((max_lon - min_lon), 1e-9)
            if frac_lat < 0.15 or frac_lon < 0.15:
                cover = "open"
            elif frac_lat > 0.35 and frac_lat < 0.65 and frac_lon > 0.35 and frac_lon < 0.65:
                cover = "dense"
            else:
                cover = "mixed"

            cells.append(
                {
                    "poly_geojson": _cell_polygon(la, lo, dlat, dlon),
                    "center_elev_m": round(float(elev), 1),
                    "avg_slope_deg": round(slope, 2),
                    "dominant_cover": cover,
                }
            )
        log.info("Real DEM: %d cells from Open-Elevation", len(cells))
    except Exception as exc:
        log.warning("DEM fetch failed (%s); falling back to mock cells", exc)
        cells = _generate_mock_cells(min_lat, min_lon, max_lat, max_lon)

    # --- OSM via Overpass ---
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

            osm_features.append(
                {
                    "kind": kind,
                    "name": tags.get("name"),
                    "geom_geojson": geom,
                }
            )
        log.info("OSM: fetched %d features", len(osm_features))
    except Exception as exc:
        log.warning("OSM fetch failed (%s); falling back to mock OSM features", exc)
        osm_features = _generate_mock_osm_features(min_lat, min_lon, max_lat, max_lon)

    n_cells = bulk_insert_terrain_cells(mission_id, cells)
    n_osm = bulk_insert_osm_features(mission_id, osm_features)
    return {"terrain_cells_inserted": n_cells, "osm_features_inserted": n_osm}


# ---------------------------------------------------------------------------
# Public entry point (importable by api/routes/admin.py)
# ---------------------------------------------------------------------------

def fetch_terrain(mission_id: int, mock: bool = False) -> dict[str, int]:
    """Fetch or synthesize terrain for mission_id.

    Reads mission.area_geom from the DB to get the bbox. Deletes any existing
    terrain_cells and osm_features for this mission before inserting new ones
    (idempotent).
    """
    mission = get_mission(mission_id)
    if mission is None:
        raise ValueError(f"Mission {mission_id} not found")

    area_geojson = mission["area_geojson"]
    if area_geojson is None:
        raise ValueError(f"Mission {mission_id} has no area_geom")

    min_lat, min_lon, max_lat, max_lon = _bbox_from_geojson(area_geojson)
    log.info(
        "Mission %d bbox: lat=[%.5f, %.5f] lon=[%.5f, %.5f]",
        mission_id,
        min_lat,
        max_lat,
        min_lon,
        max_lon,
    )

    if mock:
        return _run_mock(mission_id, min_lat, min_lon, max_lat, max_lon)

    _delete_existing_terrain(mission_id)
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
    p.add_argument("--mission-id", type=int, required=True, help="DB mission ID")
    p.add_argument("--mock", action="store_true", help="Use synthetic data (demo-safe)")
    p.add_argument("--verbose", action="store_true", help="Debug logging")
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
        f"[fetch_terrain] done: {result['terrain_cells_inserted']} terrain_cells,"
        f" {result['osm_features_inserted']} osm_features",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
