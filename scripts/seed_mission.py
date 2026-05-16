#!/usr/bin/env python3
"""Create a demo SAR mission by POSTing to /missions.

Usage:
    python scripts/seed_mission.py [--server http://localhost:8000]

Prints the mission_id, join_code, bearer_token, and seeding counts. Share the
join_code with each phone that joins.
"""
from __future__ import annotations

import argparse
import sys
import time

import requests


MISSION_NAME = "UCSC Demo"
SUBJECT_DESCRIPTION = "Test teammate, last seen near McHenry Library"
PLS_LAT = 36.9947
PLS_LON = -122.0594
DISPLAY_NAME = "Mission Creator"
CALLSIGN: str | None = None

# ~0.0009 degrees ≈ 100 m at this latitude → 200m × 200m polygon centered on PLS
_HALF_DEG = 0.0009


def build_area_polygon(center_lat: float, center_lon: float) -> dict:
    """GeoJSON Polygon, closed ring, ccw order."""
    minx = center_lon - _HALF_DEG
    maxx = center_lon + _HALF_DEG
    miny = center_lat - _HALF_DEG
    maxy = center_lat + _HALF_DEG
    return {
        "type": "Polygon",
        "coordinates": [[
            [minx, miny],
            [maxx, miny],
            [maxx, maxy],
            [minx, maxy],
            [minx, miny],
        ]],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="http://localhost:8000",
                   help="API base URL (default: http://localhost:8000)")
    args = p.parse_args()

    url = args.server.rstrip("/") + "/missions"
    body = {
        "name": MISSION_NAME,
        "subject_description": SUBJECT_DESCRIPTION,
        "pls_lat": PLS_LAT,
        "pls_lon": PLS_LON,
        "pls_ts": int(time.time()),
        "area_geojson": build_area_polygon(PLS_LAT, PLS_LON),
        "display_name": DISPLAY_NAME,
        "callsign": CALLSIGN,
    }

    print(f"POST {url}", flush=True)
    print("  (terrain seeding can take 30–60s on the real OSM/DEM path)", flush=True)

    try:
        resp = requests.post(url, json=body, timeout=180)
    except requests.RequestException as e:
        print(f"ERROR: request failed: {e}", file=sys.stderr)
        return 1

    if not resp.ok:
        print(f"ERROR: {resp.status_code}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        return 1

    data = resp.json()
    print()
    print("Mission created.")
    print(f"  mission_id:    {data['mission_id']}")
    print(f"  join_code:     {data['join_code']}      ← share this with phones")
    print(f"  bearer_token:  {data['bearer_token']}     ← creator's token, not needed by phones")
    print(f"  n_segments:    {data['n_segments']}")
    print(f"  n_hex_cells:   {data['n_hex_cells']}")
    print(f"  n_hazards:     {data['n_hazards']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
