#!/usr/bin/env python3
"""Run the 7-minute deterministic mini-mission simulation.

This is the "known-good story" before we involve an LLM:
  - Create one mission.
  - Add ALPHA, BRAVO, CHARLIE.
  - Dispatch all three.
  - Send 10-second GPS pings for 7 simulated minutes.
  - ALPHA finds an obvious clue pointing northeast.
  - Reassign CHARLIE from a low-relevance outer segment to the northeast target.
  - Verify FastAPI and the agent brief reflect that outcome.
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "dev" / "data" / f"mini_mission_{int(time.time())}.db"
sys.path.insert(0, str(REPO_ROOT))


PLS_LAT = 36.9947
PLS_LON = -122.0594
HALF_DEG = 0.0018
PING_INTERVAL_SECONDS = 10
SIM_DURATION_SECONDS = 7 * 60
FINDING_AT_SECONDS = 3 * 60 + 30
REASSIGN_AT_SECONDS = 4 * 60


def area_polygon(center_lat: float, center_lon: float) -> dict[str, Any]:
    minx = center_lon - HALF_DEG
    maxx = center_lon + HALF_DEG
    miny = center_lat - HALF_DEG
    maxy = center_lat + HALF_DEG
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


def check(name: str, condition: bool, detail: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}: {detail}", flush=True)
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def point_between(a: dict[str, float], b: dict[str, float], t: float) -> dict[str, float]:
    return {
        "lat": lerp(a["lat"], b["lat"], t),
        "lon": lerp(a["lon"], b["lon"], t),
    }


def post_ping(client, token: str, lat: float, lon: float, ts: int, battery: int) -> None:
    resp = client.post(
        "/field/ping",
        headers={"X-Bearer-Token": token},
        json={
            "lat": lat,
            "lon": lon,
            "ts": ts,
            "accuracy_m": 5,
            "speed_mps": 1.1,
            "battery_pct": battery,
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ping failed: {resp.status_code} {resp.text}")


def dispatch_action(client, token: str, dispatch_id: int, action: str) -> dict[str, Any]:
    resp = client.post(
        f"/field/dispatch/{dispatch_id}/{action}",
        headers={"X-Bearer-Token": token},
        json={} if action == "complete" else None,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"dispatch {action} failed: {resp.status_code} {resp.text}")
    return resp.json()


def load_segments(mission_id: int) -> list[dict[str, Any]]:
    from api.db import session

    with session() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.name, s.poa, s.pod, s.status,
                   X(Centroid(s.geom)) AS lon,
                   Y(Centroid(s.geom)) AS lat,
                   COALESCE((
                     SELECT COUNT(*) FROM hex_cells h
                     WHERE h.segment_id = s.id
                   ), 0) AS hex_count,
                   COALESCE((
                     SELECT COUNT(*) FROM hazards h
                     WHERE h.mission_id = s.mission_id
                       AND ST_Intersects(h.geom, s.geom)
                   ), 0) AS hazard_count
            FROM segments s
            WHERE s.mission_id = ?
            ORDER BY s.name
            """,
            (mission_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def clue_point_inside_alpha(alpha_segment_id: int, target: dict[str, Any]) -> dict[str, float]:
    """Pick a real hex centroid inside ALPHA's segment, nearest the target."""
    from api.db import session

    with session() as conn:
        row = conn.execute(
            """
            SELECT Y(Centroid(geom)) AS lat, X(Centroid(geom)) AS lon
            FROM hex_cells
            WHERE segment_id = ?
            ORDER BY Distance(Centroid(geom), MakePoint(?, ?, 4326)) ASC
            LIMIT 1
            """,
            (alpha_segment_id, target["lon"], target["lat"]),
        ).fetchone()
    if row is None:
        raise ValueError(f"No hex cells found in ALPHA segment {alpha_segment_id}")
    return {"lat": float(row["lat"]), "lon": float(row["lon"])}


def choose_story_segments(segments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pick obvious roles from the generated grid.

    Target is the northeast-most non-hazard segment. ALPHA and BRAVO start in
    the nearest neighboring segments. CHARLIE starts far away, making the later
    reassignment clear.
    """
    safe = [s for s in segments if s["hazard_count"] == 0 and s["hex_count"] > 0]
    if len(safe) < 4:
        raise ValueError(f"Need at least 4 safe segments with hex cells, got {len(safe)}")
    target = max(safe, key=lambda s: (s["lat"] - PLS_LAT) + (s["lon"] - PLS_LON))

    def dist_to_target(seg: dict[str, Any]) -> float:
        return haversine_m(seg["lat"], seg["lon"], target["lat"], target["lon"])

    near_target = sorted([s for s in safe if s["id"] != target["id"]], key=dist_to_target)
    alpha = near_target[0]
    bravo = near_target[1]
    charlie_initial = max(
        [s for s in safe if s["id"] not in {target["id"], alpha["id"], bravo["id"]}],
        key=dist_to_target,
    )
    return {
        "alpha": alpha,
        "bravo": bravo,
        "charlie_initial": charlie_initial,
        "charlie_target": target,
    }


def active_dispatch_for(client, token: str) -> dict[str, Any]:
    resp = client.get("/field/me", headers={"X-Bearer-Token": token})
    if resp.status_code != 200:
        raise RuntimeError(f"/field/me failed: {resp.status_code} {resp.text}")
    active = resp.json()["active_dispatch"]
    if active is None:
        raise RuntimeError("expected active dispatch")
    return active


def main() -> int:
    os.environ["MISSION_DB_PATH"] = str(DB_PATH)
    os.environ["TERRAIN_MOCK"] = "1"

    from fastapi.testclient import TestClient

    from api.main import app
    from agent.brief import compose_brief
    from agent.skills.read import get_findings, get_searcher
    from agent.skills.write import dispatch_searcher, reassign_searcher

    sim_start = int(time.time()) - 3600
    print(f"DB_PATH={DB_PATH}", flush=True)
    print("SIMULATION=7 minutes, 3 searchers, 10-second pings", flush=True)

    with TestClient(app) as client:
        created = client.post(
            "/missions",
            json={
                "name": "Seven Minute Northeast Clue Test",
                "subject_description": "Tired hiker in a red jacket, last seen at a trail junction",
                "pls_lat": PLS_LAT,
                "pls_lon": PLS_LON,
                "pls_ts": sim_start - 5400,
                "area_geojson": area_polygon(PLS_LAT, PLS_LON),
                "display_name": "Alpha Searcher",
                "callsign": "ALPHA",
            },
        )
        check("mission created", created.status_code == 201, f"status={created.status_code}")
        mission = created.json()
        mission_id = mission["mission_id"]
        join_code = mission["join_code"]

        people = {
            "ALPHA": {
                "user_id": mission["user_id"],
                "token": mission["bearer_token"],
            }
        }
        for callsign in ("BRAVO", "CHARLIE"):
            joined = client.post(
                "/missions/join",
                json={
                    "join_code": join_code,
                    "display_name": f"{callsign.title()} Searcher",
                    "callsign": callsign,
                },
            )
            check(f"{callsign} joined", joined.status_code == 201, f"status={joined.status_code}")
            data = joined.json()
            people[callsign] = {"user_id": data["user_id"], "token": data["bearer_token"]}

        segments = load_segments(mission_id)
        check(
            "segments seeded",
            len(segments) >= 9,
            f"segments={len(segments)} hexes={mission['n_hex_cells']} hazards={mission['n_hazards']}",
        )

        chosen = choose_story_segments(segments)
        print("STORY SEGMENTS", flush=True)
        for label, seg in chosen.items():
            print(
                f"  {label}: {seg['name']} id={seg['id']} "
                f"center=({seg['lat']:.6f},{seg['lon']:.6f}) poa={seg['poa']:.4f}",
                flush=True,
            )

        assignments = {
            "ALPHA": chosen["alpha"],
            "BRAVO": chosen["bravo"],
            "CHARLIE": chosen["charlie_initial"],
        }
        dispatches: dict[str, dict[str, Any]] = {}
        for callsign, seg in assignments.items():
            d = dispatch_searcher(
                user_id=people[callsign]["user_id"],
                segment_id=seg["id"],
                sweep_type="hasty",
                instruction=f"Search {seg['name']} using a hasty sweep.",
                reasoning=f"Mini-mission setup: {callsign} begins in {seg['name']} before the clue is found.",
                mission_id=mission_id,
            )
            dispatches[callsign] = d
            check(f"{callsign} dispatched", d["dispatch_id"] > 0, f"dispatch={d['dispatch_id']} segment={seg['name']}")

        for callsign, d in dispatches.items():
            token = people[callsign]["token"]
            ack = dispatch_action(client, token, d["dispatch_id"], "ack")
            start = dispatch_action(client, token, d["dispatch_id"], "start")
            check(
                f"{callsign} ack/start",
                ack["status"] == "acked" and start["status"] == "in_progress",
                f"ack={ack['status']} start={start['status']}",
            )

        points = {
            "PLS": {"lat": PLS_LAT, "lon": PLS_LON},
            "ALPHA": {"lat": chosen["alpha"]["lat"], "lon": chosen["alpha"]["lon"]},
            "BRAVO": {"lat": chosen["bravo"]["lat"], "lon": chosen["bravo"]["lon"]},
            "CHARLIE_INITIAL": {
                "lat": chosen["charlie_initial"]["lat"],
                "lon": chosen["charlie_initial"]["lon"],
            },
            "CHARLIE_TARGET": {
                "lat": chosen["charlie_target"]["lat"],
                "lon": chosen["charlie_target"]["lon"],
            },
        }
        clue = clue_point_inside_alpha(chosen["alpha"]["id"], chosen["charlie_target"])

        ping_counts = {"ALPHA": 0, "BRAVO": 0, "CHARLIE": 0}
        charlie_reassign: dict[str, Any] | None = None
        finding_id: int | None = None

        for offset in range(0, SIM_DURATION_SECONDS, PING_INTERVAL_SECONDS):
            ts = sim_start + offset

            alpha_t = min(1.0, offset / max(FINDING_AT_SECONDS, 1))
            alpha_point = point_between(points["PLS"], points["ALPHA"], alpha_t)
            post_ping(client, people["ALPHA"]["token"], alpha_point["lat"], alpha_point["lon"], ts, 94)
            ping_counts["ALPHA"] += 1

            bravo_t = min(1.0, offset / max(FINDING_AT_SECONDS, 1))
            bravo_point = point_between(points["PLS"], points["BRAVO"], bravo_t)
            post_ping(client, people["BRAVO"]["token"], bravo_point["lat"], bravo_point["lon"], ts, 91)
            ping_counts["BRAVO"] += 1

            if offset < REASSIGN_AT_SECONDS:
                charlie_t = min(1.0, offset / max(REASSIGN_AT_SECONDS, 1))
                charlie_point = point_between(points["PLS"], points["CHARLIE_INITIAL"], charlie_t)
            else:
                charlie_t = min(1.0, (offset - REASSIGN_AT_SECONDS) / max(SIM_DURATION_SECONDS - REASSIGN_AT_SECONDS, 1))
                charlie_point = point_between(points["CHARLIE_INITIAL"], points["CHARLIE_TARGET"], charlie_t)
            post_ping(client, people["CHARLIE"]["token"], charlie_point["lat"], charlie_point["lon"], ts, 88)
            ping_counts["CHARLIE"] += 1

            if offset == FINDING_AT_SECONDS:
                finding = client.post(
                    "/field/findings",
                    headers={"X-Bearer-Token": people["ALPHA"]["token"]},
                    json={
                        "lat": clue["lat"],
                        "lon": clue["lon"],
                        "kind": "footprint",
                        "description": "Fresh footprint pointing northeast from the trail junction.",
                        "confidence": 0.8,
                    },
                )
                check(
                    "ALPHA logged clue",
                    finding.status_code == 201,
                    f"status={finding.status_code} body={finding.text}",
                )
                finding_id = finding.json()["finding_id"]

            if offset == REASSIGN_AT_SECONDS:
                charlie_reassign = reassign_searcher(
                    user_id=people["CHARLIE"]["user_id"],
                    new_segment_id=chosen["charlie_target"]["id"],
                    sweep_type="hasty",
                    instruction=(
                        f"Leave your current outer segment and move northeast to "
                        f"{chosen['charlie_target']['name']} to follow the fresh footprint line."
                    ),
                    reasoning=(
                        "ALPHA found a fresh footprint pointing northeast. CHARLIE was farthest "
                        "from the clue on the least relevant outer assignment, so CHARLIE is the "
                        "obvious searcher to redirect to the unassigned northeast target."
                    ),
                    mission_id=mission_id,
                )
                check(
                    "CHARLIE reassigned",
                    charlie_reassign["segment_id"] == chosen["charlie_target"]["id"],
                    f"new_dispatch={charlie_reassign['dispatch_id']} target={charlie_reassign['segment_name']}",
                )
                dispatch_action(client, people["CHARLIE"]["token"], charlie_reassign["dispatch_id"], "ack")
                dispatch_action(client, people["CHARLIE"]["token"], charlie_reassign["dispatch_id"], "start")

        complete = client.post(
            f"/field/dispatch/{dispatches['ALPHA']['dispatch_id']}/complete",
            headers={"X-Bearer-Token": people["ALPHA"]["token"]},
            json={"notes": "Completed initial sweep near the clue edge."},
        )
        check("ALPHA completed initial dispatch", complete.status_code == 200, f"status={complete.status_code}")

        charlie_active = active_dispatch_for(client, people["CHARLIE"]["token"])
        check(
            "FastAPI shows CHARLIE new assignment",
            charlie_active["segment_id"] == chosen["charlie_target"]["id"],
            f"active_dispatch={charlie_active['id']} segment_id={charlie_active['segment_id']} status={charlie_active['status']}",
        )

        findings = get_findings(mission_id=mission_id, limit=5)
        check(
            "read tools see clue",
            any(f["id"] == finding_id and f["kind"] == "footprint" for f in findings),
            f"recent_findings={[(f['id'], f['kind'], f['segment_name']) for f in findings]}",
        )

        charlie = get_searcher("CHARLIE", mission_id)
        check(
            "read tools see CHARLIE reassignment",
            charlie["active_dispatch"]["segment_id"] == chosen["charlie_target"]["id"],
            f"segment={charlie['active_dispatch']['segment_name']} status={charlie['active_dispatch']['status']}",
        )

        from api.db import session

        with session() as conn:
            old = conn.execute(
                "SELECT status, superseded_by FROM dispatches WHERE id = ?",
                (dispatches["CHARLIE"]["dispatch_id"],),
            ).fetchone()
            searched = conn.execute(
                "SELECT COUNT(*) AS n FROM hex_cells WHERE mission_id = ? AND flag_searched = 1",
                (mission_id,),
            ).fetchone()
            broadcasts = conn.execute(
                "SELECT COUNT(*) AS n FROM broadcasts WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()

        check(
            "old CHARLIE dispatch superseded",
            old["status"] == "superseded" and old["superseded_by"] == charlie_reassign["dispatch_id"],
            f"old_status={old['status']} superseded_by={old['superseded_by']}",
        )
        check(
            "movement searched hexes",
            searched["n"] > 0,
            f"searched_hexes={searched['n']}",
        )

        final_brief = compose_brief(mission_id)
        check(
            "final brief mentions clue",
            "footprint" in final_brief and "CHARLIE" in final_brief,
            f"brief_chars={len(final_brief)}",
        )

        print("SUMMARY", flush=True)
        print(f"  mission_id={mission_id}", flush=True)
        print(f"  pings={ping_counts} total={sum(ping_counts.values())}", flush=True)
        print(f"  finding_id={finding_id} clue='fresh footprint pointing northeast'", flush=True)
        print(
            f"  expected_reassign=CHARLIE from {chosen['charlie_initial']['name']} "
            f"to {chosen['charlie_target']['name']}",
            flush=True,
        )
        print(
            f"  actual_reassign=CHARLIE active segment {charlie['active_dispatch']['segment_name']} "
            f"status={charlie['active_dispatch']['status']}",
            flush=True,
        )
        print(f"  searched_hexes={searched['n']} broadcasts={broadcasts['n']}", flush=True)
        print("MINI_MISSION_OK", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"MINI_MISSION_FAILED: {exc}", file=sys.stderr, flush=True)
        raise
