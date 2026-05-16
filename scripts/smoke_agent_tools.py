#!/usr/bin/env python3
"""Smoke-test the agent skills against a fresh local SQLite DB.

This intentionally uses FastAPI's TestClient instead of opening a real port:
it still runs the FastAPI route handlers and startup migrations, but it avoids
leaving a local uvicorn process running after the test.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "dev" / "data" / f"agent_tools_smoke_{int(time.time())}.db"
sys.path.insert(0, str(REPO_ROOT))


def area_polygon(center_lat: float, center_lon: float) -> dict:
    half_deg = 0.0009
    minx = center_lon - half_deg
    maxx = center_lon + half_deg
    miny = center_lat - half_deg
    maxy = center_lat + half_deg
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
        raise AssertionError(name)


def main() -> int:
    os.environ["MISSION_DB_PATH"] = str(DB_PATH)
    os.environ["TERRAIN_MOCK"] = "1"

    from fastapi.testclient import TestClient

    from api.main import app
    from agent.brief import compose_brief
    from agent.skills.read import (
        get_mission_overview,
        get_searcher,
        get_segment,
        get_uncovered_areas,
        list_searchers,
        query_route,
    )
    from agent.skills.write import broadcast, dispatch_searcher
    from workers.agent import run_once

    print(f"DB_PATH={DB_PATH}", flush=True)

    with TestClient(app) as client:
        body = {
            "name": "Agent Tool Smoke Test",
            "subject_description": "Demo hiker in red jacket",
            "pls_lat": 36.9947,
            "pls_lon": -122.0594,
            "pls_ts": int(time.time()) - 5400,
            "area_geojson": area_polygon(36.9947, -122.0594),
            "display_name": "Tool Tester",
            "callsign": "ALPHA",
        }

        created = client.post("/missions", json=body)
        check("POST /missions", created.status_code == 201, f"status={created.status_code}")
        mission = created.json()
        mission_id = mission["mission_id"]
        token = mission["bearer_token"]
        user_id = mission["user_id"]
        check(
            "mission seeded",
            mission["n_segments"] > 0 and mission["n_hex_cells"] > 0,
            f"segments={mission['n_segments']} hexes={mission['n_hex_cells']} hazards={mission['n_hazards']}",
        )

        overview = get_mission_overview(mission_id)
        check(
            "get_mission_overview",
            overview["id"] == mission_id and overview["total_segments"] > 0,
            f"name={overview['name']} searchers={overview['total_searchers']} segments={overview['total_segments']}",
        )

        searchers = list_searchers(mission_id)
        check(
            "list_searchers",
            len(searchers) == 1 and searchers[0]["id"] == user_id,
            f"count={len(searchers)} callsign={searchers[0]['callsign']}",
        )

        uncovered = get_uncovered_areas(mission_id=mission_id, limit=3)
        check(
            "get_uncovered_areas",
            len(uncovered) > 0,
            f"top={uncovered[0]['name']} remaining={uncovered[0]['remaining_probability']:.4f}",
        )

        segment_id = uncovered[0]["id"]
        segment = get_segment(segment_id, mission_id)
        check(
            "get_segment",
            segment["id"] == segment_id,
            f"name={segment['name']} poa={segment['poa']} pod={segment['pod']}",
        )

        searcher = get_searcher(user_id, mission_id)
        check(
            "get_searcher",
            searcher["id"] == user_id,
            f"status={searcher['status']} track_pings={searcher['track_last_30m']['ping_count']}",
        )

        route = query_route(
            body["pls_lat"],
            body["pls_lon"],
            segment["center_lat"],
            segment["center_lon"],
            mission_id,
        )
        check(
            "query_route",
            len(route["waypoints"]) >= 2,
            f"waypoints={len(route['waypoints'])} snapped={route['snapped']}",
        )

        brief = compose_brief(mission_id)
        check(
            "compose_brief",
            "Mission Brief" in brief and "Coverage Summary" in brief and "Searchers" in brief,
            f"chars={len(brief)}",
        )

        dispatch = dispatch_searcher(
            user_id=user_id,
            segment_id=segment_id,
            sweep_type="hasty",
            instruction=f"Proceed to {segment['name']} and begin a hasty sweep.",
            reasoning="Smoke test: assign the only searcher to an available segment.",
            mission_id=mission_id,
        )
        check(
            "dispatch_searcher",
            dispatch["dispatch_id"] > 0,
            f"dispatch_id={dispatch['dispatch_id']} segment={dispatch['segment_name']}",
        )

        all_hands = broadcast(
            scope="all",
            kind="info",
            message="Smoke test broadcast: agent tools are connected.",
            reasoning="Smoke test: verify broadcasts written by agent tools appear through FastAPI.",
            mission_id=mission_id,
        )
        check(
            "broadcast",
            all_hands["broadcast_id"] > 0,
            f"broadcast_id={all_hands['broadcast_id']} scope={all_hands['scope']}",
        )

        me = client.get("/field/me", headers={"X-Bearer-Token": token})
        check("/field/me after tool writes", me.status_code == 200, f"status={me.status_code}")
        me_json = me.json()
        active = me_json["active_dispatch"]
        check(
            "FastAPI sees dispatch",
            active is not None and active["id"] == dispatch["dispatch_id"],
            f"active_dispatch_id={active['id'] if active else None}",
        )
        check(
            "FastAPI sees broadcasts",
            len(me_json["recent_broadcasts"]) >= 2,
            f"recent_broadcasts={len(me_json['recent_broadcasts'])}",
        )

        ack = client.post(
            f"/field/dispatch/{dispatch['dispatch_id']}/ack",
            headers={"X-Bearer-Token": token},
        )
        check("dispatch ack endpoint", ack.status_code == 200, f"status={ack.status_code} body={ack.json()}")

        worker_status = run_once(
            mission_id=mission_id,
            dry_run=True,
            force=True,
            prompt_path=REPO_ROOT / "openclaw" / "agent_prompt.md",
            event_window_seconds=90,
            timeout_seconds=30,
        )
        check("workers.agent dry run", worker_status == 0, f"exit_code={worker_status}")

    print("SMOKE_AGENT_TOOLS_OK", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"SMOKE_AGENT_TOOLS_FAILED: {exc}", file=sys.stderr, flush=True)
        raise
