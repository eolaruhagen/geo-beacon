#!/usr/bin/env python3
"""Demo searcher simulator — drives the observer user's pings toward their
current dispatch target so the demo map shows the user 'walking' when the
agent dispatches them. Pings only; lifecycle (ack/start/complete) and
findings stay manual."""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import time
import traceback
from typing import Optional

from api.db import session
from api.db.hex_cells import hex_cell_id_at, mark_hex_searched
from api.db.pings import insert_ping
from scripts.apply_migrations import apply, DEFAULT_DB_PATH, DEFAULT_MIGRATIONS_DIR


R_LAT_M = 111_000  # meters per degree latitude (~constant)
DB_LOCK_RETRIES = 3
DB_LOCK_BACKOFF_S = 0.2
MIN_TICK_S = 0.1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive the observer user's pings toward their current dispatch."
    )
    p.add_argument("--step-m", type=float, default=6.0,
                   help="Step distance per tick in meters (default: 6.0)")
    p.add_argument("--tick-s", type=float, default=1.0,
                   help="Sleep between steps in seconds (default: 1.0)")
    p.add_argument("--arrival-m", type=float, default=4.0,
                   help="Distance below which we consider arrived (default: 4.0)")
    p.add_argument("--mission-id", type=int, default=None,
                   help="If set, only sim if observer user is in that mission.")
    args = p.parse_args()
    if args.tick_s < MIN_TICK_S:
        args.tick_s = MIN_TICK_S
    return args


def find_observer(mission_id_filter: Optional[int]) -> Optional[tuple[int, int]]:
    """Return (user_id, mission_id) for the single observer user, or None."""
    with session() as conn:
        row = conn.execute(
            "SELECT id, current_mission_id FROM users WHERE is_observer = 1 LIMIT 1"
        ).fetchone()
        if not row:
            return None
        user_id = row["id"]
        mission_id = row["current_mission_id"]
        if mission_id is None:
            return None
        if mission_id_filter is not None and mission_id != mission_id_filter:
            return None
        return (user_id, mission_id)


def latest_ping(user_id: int, mission_id: int) -> Optional[tuple[float, float]]:
    with session() as conn:
        row = conn.execute(
            """
            SELECT lat, lon FROM pings
            WHERE user_id = ? AND mission_id = ?
            ORDER BY ts DESC LIMIT 1
            """,
            (user_id, mission_id),
        ).fetchone()
        if not row:
            return None
        return (row["lat"], row["lon"])


def active_dispatch(user_id: int, mission_id: int) -> Optional[dict]:
    with session() as conn:
        row = conn.execute(
            """
            SELECT id, entry_lat, entry_lon, status FROM dispatches
            WHERE user_id = ? AND mission_id = ?
              AND status IN ('pending', 'acked', 'in_progress')
            ORDER BY issued_ts DESC LIMIT 1
            """,
            (user_id, mission_id),
        ).fetchone()
        return dict(row) if row else None


def mission_bbox(mission_id: int) -> Optional[tuple[float, float, float, float]]:
    """(minx, miny, maxx, maxy) in (lon_min, lat_min, lon_max, lat_max)."""
    with session() as conn:
        row = conn.execute(
            """
            SELECT MbrMinX(area_geom) AS minx, MbrMinY(area_geom) AS miny,
                   MbrMaxX(area_geom) AS maxx, MbrMaxY(area_geom) AS maxy
            FROM missions WHERE id = ?
            """,
            (mission_id,),
        ).fetchone()
        if not row or row["minx"] is None:
            return None
        return (row["minx"], row["miny"], row["maxx"], row["maxy"])


def mission_centroid(mission_id: int) -> Optional[tuple[float, float]]:
    """Returns (lat, lon) of centroid of mission area, or None."""
    with session() as conn:
        row = conn.execute(
            """
            SELECT X(Centroid(area_geom)) AS lon, Y(Centroid(area_geom)) AS lat
            FROM missions WHERE id = ?
            """,
            (mission_id,),
        ).fetchone()
        if not row or row["lat"] is None:
            return None
        return (row["lat"], row["lon"])


def step_toward(cur_lat, cur_lon, tgt_lat, tgt_lon, step_m) -> tuple[float, float]:
    """Return (new_lat, new_lon) one step of step_m toward target.
    If distance to target is <= step_m, snap to target."""
    r_lon_m = R_LAT_M * math.cos(math.radians(cur_lat))
    dlat_m = (tgt_lat - cur_lat) * R_LAT_M
    dlon_m = (tgt_lon - cur_lon) * r_lon_m
    dist_m = math.hypot(dlat_m, dlon_m)
    if dist_m <= step_m:
        return tgt_lat, tgt_lon
    frac = step_m / dist_m
    return (cur_lat + (tgt_lat - cur_lat) * frac,
            cur_lon + (tgt_lon - cur_lon) * frac)


def distance_m(lat1, lon1, lat2, lon2) -> float:
    r_lon_m = R_LAT_M * math.cos(math.radians(lat1))
    dlat_m = (lat2 - lat1) * R_LAT_M
    dlon_m = (lon2 - lon1) * r_lon_m
    return math.hypot(dlat_m, dlon_m)


def clamp_to_bbox(lat, lon, minx, miny, maxx, maxy) -> tuple[float, float, bool]:
    """Returns (clamped_lat, clamped_lon, was_clamped). minx/maxx = lon, miny/maxy = lat."""
    clamped_lon = min(max(lon, minx), maxx)
    clamped_lat = min(max(lat, miny), maxy)
    was = (clamped_lon != lon) or (clamped_lat != lat)
    return (clamped_lat, clamped_lon, was)


def write_ping(user_id: int, mission_id: int, lat: float, lon: float) -> Optional[int]:
    """Insert ping + best-effort mark hex searched. Returns ping_id or None on failure."""
    ts = int(time.time())
    ping_id: Optional[int] = None
    last_err: Optional[Exception] = None
    for attempt in range(DB_LOCK_RETRIES):
        try:
            ping_id = insert_ping(
                user_id=user_id,
                mission_id=mission_id,
                lat=lat,
                lon=lon,
                ts=ts,
                source="replay",
            )
            break
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(DB_LOCK_BACKOFF_S)
    if ping_id is None:
        print(f"[sim] error: insert_ping failed after retries: {last_err}", flush=True)
        return None

    try:
        hex_id = hex_cell_id_at(mission_id, lat, lon)
        if hex_id is not None:
            mark_hex_searched(hex_id, user_id, ts)
    except Exception as e:
        print(f"[sim] error: hex coverage update failed for ping {ping_id}: {e}",
              flush=True)

    return ping_id


def tick(args: argparse.Namespace, state: dict) -> None:
    """One iteration of the loop. Mutates `state` for de-noised logging."""
    found = find_observer(args.mission_id)
    if found is None:
        if state.get("last_status") != "no_observer":
            print("[sim] no observer user found yet; waiting for DB restore",
                  flush=True)
            state["last_status"] = "no_observer"
        return
    user_id, mission_id = found

    if state.get("last_status") in (None, "no_observer") or \
       state.get("last_user") != (user_id, mission_id):
        print(f"[sim] observer user_id={user_id} mission={mission_id} starting",
              flush=True)
        state["last_user"] = (user_id, mission_id)
        state["last_status"] = "started"

    # Current position. Seed if no pings yet.
    cur = latest_ping(user_id, mission_id)
    dispatch = active_dispatch(user_id, mission_id)

    if cur is None:
        if dispatch is not None:
            seed_lat, seed_lon = dispatch["entry_lat"], dispatch["entry_lon"]
            print(f"[sim] seeding first ping at dispatch entry "
                  f"({seed_lat:.6f}, {seed_lon:.6f})", flush=True)
        else:
            centroid = mission_centroid(mission_id)
            if centroid is None:
                print("[sim] error: cannot seed — mission has no area_geom centroid",
                      flush=True)
                return
            seed_lat, seed_lon = centroid
            print(f"[sim] seeding first ping at mission centroid "
                  f"({seed_lat:.6f}, {seed_lon:.6f})", flush=True)
        write_ping(user_id, mission_id, seed_lat, seed_lon)
        return

    cur_lat, cur_lon = cur

    if dispatch is None:
        if state.get("last_status") != "idle":
            print("[sim] no active dispatch, idling", flush=True)
            state["last_status"] = "idle"
        return

    tgt_lat = dispatch["entry_lat"]
    tgt_lon = dispatch["entry_lon"]
    dist = distance_m(cur_lat, cur_lon, tgt_lat, tgt_lon)

    if dist <= args.arrival_m:
        if state.get("last_status") != f"arrived:{dispatch['id']}":
            print(f"[sim] arrived at dispatch={dispatch['id']} target "
                  f"({tgt_lat:.6f}, {tgt_lon:.6f})", flush=True)
            state["last_status"] = f"arrived:{dispatch['id']}"
        return

    new_lat, new_lon = step_toward(cur_lat, cur_lon, tgt_lat, tgt_lon, args.step_m)

    bbox = mission_bbox(mission_id)
    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        new_lat, new_lon, was_clamped = clamp_to_bbox(
            new_lat, new_lon, minx, miny, maxx, maxy
        )
        if was_clamped:
            print("[sim] clamped to mission bbox", flush=True)

    ping_id = write_ping(user_id, mission_id, new_lat, new_lon)
    if ping_id is None:
        return

    remaining = distance_m(new_lat, new_lon, tgt_lat, tgt_lon)
    print(f"[sim] step → ({new_lat:.6f}, {new_lon:.6f}) "
          f"toward dispatch={dispatch['id']} ({remaining:.1f} m left)",
          flush=True)
    state["last_status"] = f"moving:{dispatch['id']}"


def main() -> int:
    args = parse_args()
    apply(os.environ.get("MISSION_DB_PATH", DEFAULT_DB_PATH), DEFAULT_MIGRATIONS_DIR)
    state: dict = {"last_status": None}
    print(f"[sim] starting loop step_m={args.step_m} tick_s={args.tick_s} "
          f"arrival_m={args.arrival_m} mission_id={args.mission_id}", flush=True)
    try:
        while True:
            try:
                tick(args, state)
            except Exception:
                traceback.print_exc()
            time.sleep(args.tick_s)
    except KeyboardInterrupt:
        print("[sim] interrupted, exiting", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
