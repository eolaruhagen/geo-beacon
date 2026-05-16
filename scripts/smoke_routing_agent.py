#!/usr/bin/env python3
"""Smoke-test the per-searcher routing worker without spending LLM tokens."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.db import session
from api.db.hex_cells import (
    bulk_insert_hex_cells,
    hex_cell_id_at,
    mark_hex_searched,
    set_flag_clue_for_hex,
)
from api.db.missions import create_mission, set_status
from api.db.pings import insert_ping
from api.db.segments import bulk_insert_segments
from api.db.users import create_user, set_current_mission
from scripts.apply_migrations import apply, DEFAULT_MIGRATIONS_DIR
from scripts.fetch_terrain import _generate_mock_hex_data
from workers.agent import run_tick


def _area(center_lat: float, center_lon: float, half: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [center_lon - half, center_lat - half],
            [center_lon + half, center_lat - half],
            [center_lon + half, center_lat + half],
            [center_lon - half, center_lat + half],
            [center_lon - half, center_lat - half],
        ]],
    }


def seed(db_path: Path) -> dict:
    os.environ["MISSION_DB_PATH"] = str(db_path)
    apply(str(db_path), DEFAULT_MIGRATIONS_DIR)

    center_lat, center_lon = 37.2868, -122.1842
    half = 0.0013
    area = _area(center_lat, center_lon, half)

    admin = create_user("Admin", "ADMIN")
    mission_id = create_mission(
        name="Routing Smoke",
        subject_description="Test subject",
        pls_lat=center_lat,
        pls_lon=center_lon,
        pls_ts=int(time.time()) - 600,
        area_geojson=area,
        created_by_user_id=admin["id"],
        join_code=f"SM{int(time.time()) % 10000:04d}",
    )
    set_current_mission(admin["id"], mission_id)
    set_status(mission_id, "active")

    segment_id = bulk_insert_segments(
        mission_id,
        [{
            "name": "ALL",
            "area_m2": 10_000,
            "poa": 1.0,
            "avg_slope_deg": 5.0,
            "dominant_cover": "mixed",
            "trail_length_m": 0.0,
            "poly_geojson": area,
        }],
    )[0]

    hexes = _generate_mock_hex_data(
        center_lat - half,
        center_lon - half,
        center_lat + half,
        center_lon + half,
        [],
    )
    for hex_row in hexes:
        hex_row["segment_id"] = segment_id
    bulk_insert_hex_cells(mission_id, hexes)

    users = []
    offsets = [
        ("Alpha", "ALPHA", 0.0, 0.0),
        ("Bravo", "BRAVO", 0.00012, 0.00010),
        ("Charlie", "CHARLIE", -0.00010, -0.00012),
    ]
    for name, callsign, dlat, dlon in offsets:
        user = create_user(name, callsign, current_mission_id=mission_id)
        users.append(user)
        lat = center_lat + dlat
        lon = center_lon + dlon
        ts = int(time.time())
        insert_ping(user["id"], mission_id, lat, lon, ts, source="replay")
        hex_id = hex_cell_id_at(mission_id, lat, lon)
        if hex_id is not None:
            mark_hex_searched(hex_id, user["id"], ts)

    clue_lat = center_lat + 0.00032
    clue_lon = center_lon + 0.00032
    clue_hex = hex_cell_id_at(mission_id, clue_lat, clue_lon)
    if clue_hex is not None:
        set_flag_clue_for_hex(clue_hex)
        with session() as conn:
            conn.execute(
                """
                INSERT INTO findings (
                    mission_id, reporter_user_id, hex_id, ts, lat, lon,
                    kind, description, confidence, geom
                )
                VALUES (?, ?, ?, ?, ?, ?, 'clue', ?, 0.8, SetSRID(MakePoint(?, ?), 4326))
                """,
                (
                    mission_id,
                    users[0]["id"],
                    clue_hex,
                    int(time.time()),
                    clue_lat,
                    clue_lon,
                    "orange cloth tied to brush",
                    clue_lon,
                    clue_lat,
                ),
            )

    return {
        "mission_id": mission_id,
        "user_ids": [user["id"] for user in users],
        "hex_count": len(hexes),
        "clue_hex": clue_hex,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        default=REPO_ROOT / "dev" / "data" / f"routing_agent_smoke_{int(time.time())}.db",
    )
    parser.add_argument("--print-payloads", action="store_true")
    args = parser.parse_args()

    db_path = args.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()

    seed_info = seed(db_path)
    worker_args = argparse.Namespace(
        mission_id=seed_info["mission_id"],
        user_id=None,
        max_searchers=3,
        parallelism=3,
        timeout_seconds=None,
        interval_seconds=60,
        loop=False,
        skip_active=False,
        dry_run=False,
        payloads_only=False,
        print_payloads=args.print_payloads,
        fallback_heuristic=True,
        mode="heuristic",
    )
    results = run_tick(worker_args)
    with session() as conn:
        counts = dict(conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM dispatches) AS dispatches,
              (SELECT COUNT(*) FROM dispatches WHERE segment_id IS NULL) AS cell_dispatches,
              (SELECT COUNT(*) FROM broadcasts) AS broadcasts
            """
        ).fetchone())
        token = conn.execute(
            "SELECT bearer_token FROM users WHERE id = ?",
            (seed_info["user_ids"][0],),
        ).fetchone()["bearer_token"]

    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        route_resp = client.get("/field/me/route", headers={"X-Bearer-Token": token})
        route_body = route_resp.json()

    print(json.dumps({
        "seed": seed_info,
        "results": [r.__dict__ for r in results],
        "counts": counts,
        "route_status": route_resp.status_code,
        "route": route_body,
    }, indent=2))

    if counts["cell_dispatches"] != 3:
        raise SystemExit("expected 3 cell dispatches")
    if route_resp.status_code != 200 or len(route_body.get("waypoints", [])) < 2:
        raise SystemExit("expected active cell dispatch route")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
