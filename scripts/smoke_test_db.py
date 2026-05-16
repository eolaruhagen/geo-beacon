#!/usr/bin/env python3
"""Smoke test for all api/db/ helper modules."""
import os, sys, json, time
from pathlib import Path

DB_PATH = "/tmp/db_smoke_all.db"
os.environ["MISSION_DB_PATH"] = DB_PATH

# Clean up from previous run
if Path(DB_PATH).exists():
    Path(DB_PATH).unlink()

# Apply migrations first
from scripts.apply_migrations import apply
apply(DB_PATH, Path("migrations"))

# ---- missions ----
from api.db.missions import create_mission, get_mission, set_status, active_mission_id

area = {
    "type": "Polygon",
    "coordinates": [[
        [-122.0, 37.0], [-122.0, 37.1], [-121.9, 37.1],
        [-121.9, 37.0], [-122.0, 37.0]
    ]]
}
mid = create_mission("Test SAR", "Lost hiker, blue jacket", 37.05, -121.95, int(time.time()), area)
assert isinstance(mid, int) and mid > 0, f"create_mission failed: {mid}"
print(f"  create_mission -> id={mid}")

m = get_mission(mid)
assert m is not None and m["name"] == "Test SAR"
assert isinstance(m["area_geojson"], dict) and m["area_geojson"]["type"] == "Polygon"
print(f"  get_mission -> ok, area_geojson type={m['area_geojson']['type']}")

assert active_mission_id() is None, "should be None before set active"
set_status(mid, "active")
assert active_mission_id() == mid, "active_mission_id should return mid"
print(f"  set_status + active_mission_id -> ok")

set_status(mid, "ended")
m2 = get_mission(mid)
assert m2["status"] == "ended" and m2["ended_ts"] is not None
print(f"  set_status ended -> ended_ts={m2['ended_ts']}")

# Reset to active for rest of test
set_status(mid, "active")

# ---- users ----
from api.db.users import create_user, get_user_by_token, get_user

u = create_user("Alice Smith", "Alpha", "searcher")
assert "bearer_token" in u and len(u["bearer_token"]) == 64
print(f"  create_user -> id={u['id']}, callsign={u['callsign']}")

u2 = get_user_by_token(u["bearer_token"])
assert u2 is not None and u2["id"] == u["id"]
print(f"  get_user_by_token -> ok")

u3 = get_user(u["id"])
assert u3 is not None and u3["display_name"] == "Alice Smith"
print(f"  get_user -> ok")

assert get_user_by_token("badtoken") is None
print(f"  get_user_by_token(bad) -> None ok")

# ---- pings ----
from api.db.pings import insert_ping

now = int(time.time())
pid = insert_ping(u["id"], mid, 37.05, -121.95, now, accuracy_m=5.0, source="phone")
assert isinstance(pid, int) and pid > 0
print(f"  insert_ping -> id={pid}")

pid2 = insert_ping(u["id"], mid, 37.051, -121.951, now + 60)
assert pid2 > pid
print(f"  insert_ping #2 -> id={pid2}")

# ---- segments ----
from api.db.segments import bulk_insert_segments, segments_for_mission

seg_rows = [
    {
        "name": "Seg-A",
        "poly_geojson": {
            "type": "Polygon",
            "coordinates": [[
                [-122.0, 37.0], [-122.0, 37.05], [-121.95, 37.05],
                [-121.95, 37.0], [-122.0, 37.0]
            ]]
        },
        "area_m2": 25000000.0,
        "poa": 0.6,
        "avg_slope_deg": 5.0,
        "dominant_cover": "open",
        "trail_length_m": 500.0,
    },
    {
        "name": "Seg-B",
        "poly_geojson": {
            "type": "Polygon",
            "coordinates": [[
                [-121.95, 37.0], [-121.95, 37.05], [-121.9, 37.05],
                [-121.9, 37.0], [-121.95, 37.0]
            ]]
        },
        "area_m2": 25000000.0,
        "poa": 0.4,
        "avg_slope_deg": 12.0,
        "dominant_cover": "mixed",
        "trail_length_m": 0.0,
    },
]
n = bulk_insert_segments(mid, seg_rows)
assert n == 2
print(f"  bulk_insert_segments -> {n}")

segs = segments_for_mission(mid)
assert len(segs) == 2
assert all(isinstance(s["geom_geojson"], dict) for s in segs)
print(f"  segments_for_mission -> {len(segs)} rows, geom_geojson ok")

# ---- terrain ----
from api.db.terrain import (
    bulk_insert_terrain_cells, bulk_insert_osm_features,
    terrain_cells_for_mission, osm_features_for_mission
)

cell_poly = {
    "type": "Polygon",
    "coordinates": [[
        [-122.0, 37.0], [-122.0, 37.001], [-121.999, 37.001],
        [-121.999, 37.0], [-122.0, 37.0]
    ]]
}
cells = [{"poly_geojson": cell_poly, "center_elev_m": 350.0, "avg_slope_deg": 3.0, "dominant_cover": "open"}]
nc = bulk_insert_terrain_cells(mid, cells)
assert nc == 1
print(f"  bulk_insert_terrain_cells -> {nc}")

trail_geom = {
    "type": "LineString",
    "coordinates": [[-122.0, 37.0], [-121.95, 37.05]]
}
osm = [{"kind": "trail", "name": "Ridge Trail", "geom_geojson": trail_geom}]
no = bulk_insert_osm_features(mid, osm)
assert no == 1
print(f"  bulk_insert_osm_features -> {no}")

tc = terrain_cells_for_mission(mid)
assert len(tc) == 1 and isinstance(tc[0]["poly_geojson"], dict)
print(f"  terrain_cells_for_mission -> {len(tc)} rows ok")

of = osm_features_for_mission(mid)
assert len(of) == 1 and of[0]["name"] == "Ridge Trail"
print(f"  osm_features_for_mission -> {len(of)} rows ok")

# ---- gate ----
from api.db.gate import enqueue_trigger

qid = enqueue_trigger(mid, "mission_start", {"note": "test"})
assert isinstance(qid, int) and qid > 0
print(f"  enqueue_trigger -> id={qid}")

qid2 = enqueue_trigger(mid, "divergence")
assert qid2 > qid
print(f"  enqueue_trigger (no context) -> id={qid2}")

# ---- geojson ----
from api.db.geojson import mission_state_feature_collection

fc = mission_state_feature_collection(mid)
assert fc["type"] == "FeatureCollection"
assert isinstance(fc["features"], list)
types = [f["properties"]["feature_type"] for f in fc["features"]]
print(f"  mission_state_feature_collection -> {len(fc['features'])} features, types={set(types)}")
assert "segment" in types
assert "searcher" in types
assert "track" in types

print()
print("ALL SMOKE TESTS PASSED")
