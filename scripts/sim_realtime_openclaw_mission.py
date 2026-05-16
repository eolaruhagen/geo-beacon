#!/usr/bin/env python3
"""Run a stepped SAR simulation with minute-by-minute OpenClaw turns.

This is closer to the real hackathon architecture than the one-shot demos:

* field pings are written to SQLite every few simulated seconds
* findings/hazards arrive during the mission, not all at the end
* once per simulated minute, a fresh brief is composed from SQLite
* OpenClaw receives that brief and can call MCP tools to read/write SQLite
* after each OpenClaw turn, the script records the exact DB changes

The script is designed for the DGX host. It expects:

* this repo checked out on the DGX
* the Python virtualenv and SpatiaLite package already working
* the OpenClaw/NemoClaw sandbox container already running
* OpenClaw configured with the geo-beacon-sar MCP server at 172.17.0.1:8765

Example:

    MISSION_DB_PATH=/tmp/ignored.db \
      python scripts/sim_realtime_openclaw_mission.py --minutes 10
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPATIALITE = (
    REPO_ROOT
    / "dev/data/spatialite_pkg/root/usr/lib/aarch64-linux-gnu/mod_spatialite.so"
)
DEFAULT_MCP_HOST = "172.17.0.1"
DEFAULT_MCP_PORT = 8765

PLS_LAT = 37.28680
PLS_LON = -122.18420
HALF_LAT = 0.0080
HALF_LON = 0.0100


OPENCLAW_STDIN_RUNNER = r"""
import os
import pathlib
import subprocess
import sys

prompt = sys.stdin.read()
session_id = os.environ.get("GB_OPENCLAW_SESSION_ID", "geo-beacon-realtime")
thinking = os.environ.get("GB_OPENCLAW_THINKING", "off")
timeout = os.environ.get("GB_OPENCLAW_TIMEOUT", "900")

env = os.environ.copy()
try:
    raw = subprocess.check_output(
        ["sh", "-lc", ". /tmp/nemoclaw-proxy-env.sh >/dev/null 2>&1 || true; env -0"]
    )
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode()] = value.decode(errors="ignore")
except Exception as exc:
    print(f"warning: could not load proxy env: {exc}", file=sys.stderr)

# The proxy env can report HOME=/root in this container. OpenClaw must use
# the sandbox user's real home or plugin/runtime ownership breaks.
env["HOME"] = "/sandbox"

cmd = [
    "openclaw",
    "agent",
    "--agent",
    "main",
    "--session-id",
    session_id,
    "--thinking",
    thinking,
    "--timeout",
    timeout,
    "--json",
    "--message",
    prompt,
]

proc = subprocess.run(cmd, env=env, text=True, capture_output=True)
if proc.stdout:
    print(proc.stdout, end="")
if proc.stderr:
    print(proc.stderr, end="", file=sys.stderr)
sys.exit(proc.returncode)
"""


@dataclass
class UserState:
    callsign: str
    user_id: int
    token: str
    lat: float
    lon: float


def log(event: str, **data: Any) -> None:
    payload = {"event": event, "ts": int(time.time()), **data}
    print(json.dumps(payload, sort_keys=True), flush=True)


def area_polygon() -> dict[str, Any]:
    minx = PLS_LON - HALF_LON
    maxx = PLS_LON + HALF_LON
    miny = PLS_LAT - HALF_LAT
    maxy = PLS_LAT + HALF_LAT
    return {
        "type": "Polygon",
        "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
    }


def box_poly(center_lat: float, center_lon: float, half_lat: float, half_lon: float) -> dict[str, Any]:
    return {
        "type": "Polygon",
        "coordinates": [[
            [center_lon - half_lon, center_lat - half_lat],
            [center_lon + half_lon, center_lat - half_lat],
            [center_lon + half_lon, center_lat + half_lat],
            [center_lon - half_lon, center_lat + half_lat],
            [center_lon - half_lon, center_lat - half_lat],
        ]],
    }


def line(points: list[tuple[float, float]]) -> dict[str, Any]:
    return {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in points]}


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def tiny_detour(callsign: str, minute: int, step: int) -> tuple[float, float]:
    """Small deterministic wiggle so tracks are not perfectly straight."""
    seed = sum(ord(c) for c in callsign) + minute * 17 + step * 5
    angle = (seed % 360) * math.pi / 180.0
    amp = 0.000055 + (seed % 5) * 0.000006
    return math.sin(angle) * amp, math.cos(angle) * amp


def configure_env(db_path: Path) -> None:
    os.environ["MISSION_DB_PATH"] = str(db_path)
    os.environ["TERRAIN_MOCK"] = "1"
    if DEFAULT_SPATIALITE.exists():
        os.environ["SPATIALITE_PATH"] = str(DEFAULT_SPATIALITE)
        spatialite_dir = str(DEFAULT_SPATIALITE.parent)
        os.environ["LD_LIBRARY_PATH"] = (
            spatialite_dir
            if not os.environ.get("LD_LIBRARY_PATH")
            else f"{spatialite_dir}:{os.environ['LD_LIBRARY_PATH']}"
        )


def db_segment_rows(mission_id: int) -> list[dict[str, Any]]:
    from api.db import session

    with session() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.name, s.poa, s.pod, s.status, s.avg_slope_deg,
                   s.dominant_cover, s.trail_length_m,
                   X(Centroid(s.geom)) AS lon, Y(Centroid(s.geom)) AS lat,
                   COALESCE((SELECT COUNT(*) FROM hex_cells h WHERE h.segment_id = s.id), 0) AS hex_count,
                   COALESCE((
                       SELECT COUNT(*) FROM hazards h
                       WHERE h.mission_id = s.mission_id AND ST_Intersects(h.geom, s.geom)
                   ), 0) AS hazard_count
            FROM segments s
            WHERE s.mission_id = ?
            ORDER BY s.name
            """,
            (mission_id,),
        ).fetchall()
    return [dict(row) for row in rows if row["hex_count"] > 0]


def choose_unique_segments(
    segments: list[dict[str, Any]],
    targets: list[tuple[str, tuple[float, float]]],
) -> dict[str, dict[str, Any]]:
    min_lat = min(s["lat"] for s in segments)
    max_lat = max(s["lat"] for s in segments)
    min_lon = min(s["lon"] for s in segments)
    max_lon = max(s["lon"] for s in segments)
    chosen: dict[str, dict[str, Any]] = {}
    used: set[int] = set()

    def normalized(seg: dict[str, Any]) -> tuple[float, float]:
        return (
            (seg["lat"] - min_lat) / max(max_lat - min_lat, 1e-9),
            (seg["lon"] - min_lon) / max(max_lon - min_lon, 1e-9),
        )

    for label, (target_lat_n, target_lon_n) in targets:
        candidates = [s for s in segments if s["id"] not in used]

        def score(seg: dict[str, Any]) -> float:
            lat_n, lon_n = normalized(seg)
            hazard_penalty = 0.018 * seg["hazard_count"]
            trail_bonus = -0.0008 if seg["trail_length_m"] > 0 else 0.0
            return (lat_n - target_lat_n) ** 2 + (lon_n - target_lon_n) ** 2 + hazard_penalty + trail_bonus

        picked = min(candidates, key=score)
        chosen[label] = picked
        used.add(int(picked["id"]))

    return chosen


def dispatch_action(client, token: str, dispatch_id: int, action: str) -> dict[str, Any]:
    body = {"notes": "realtime simulation transition"} if action == "complete" else None
    resp = client.post(
        f"/field/dispatch/{dispatch_id}/{action}",
        headers={"X-Bearer-Token": token},
        json=body,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"dispatch {action} failed: {resp.status_code} {resp.text}")
    return resp.json()


def post_ping(
    client,
    token: str,
    lat: float,
    lon: float,
    ts: int,
    battery: int,
    speed_mps: float,
) -> int:
    resp = client.post(
        "/field/ping",
        headers={"X-Bearer-Token": token},
        json={
            "lat": lat,
            "lon": lon,
            "ts": ts,
            "accuracy_m": 4.0,
            "speed_mps": speed_mps,
            "battery_pct": battery,
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ping failed: {resp.status_code} {resp.text}")
    return int(resp.json()["ping_id"])


def log_finding(
    client,
    token: str,
    lat: float,
    lon: float,
    kind: str,
    description: str,
    confidence: float,
) -> int:
    resp = client.post(
        "/field/findings",
        headers={"X-Bearer-Token": token},
        json={
            "lat": lat,
            "lon": lon,
            "kind": kind,
            "description": description,
            "confidence": confidence,
        },
    )
    if resp.status_code != 201:
        raise RuntimeError(f"finding failed: {resp.status_code} {resp.text}")
    return int(resp.json()["finding_id"])


def active_dispatch_for_user(user_id: int, mission_id: int) -> dict[str, Any] | None:
    from api.db import session

    with session() as conn:
        row = conn.execute(
            """
            SELECT d.id, d.segment_id, d.status, d.sweep_type, d.entry_lat, d.entry_lon,
                   s.name AS segment_name,
                   X(Centroid(s.geom)) AS segment_lon,
                   Y(Centroid(s.geom)) AS segment_lat
            FROM dispatches d
            LEFT JOIN segments s ON s.id = d.segment_id
            WHERE d.user_id = ?
              AND d.mission_id = ?
              AND d.status IN ('pending', 'acked', 'in_progress')
            ORDER BY d.issued_ts DESC
            LIMIT 1
            """,
            (user_id, mission_id),
        ).fetchone()
    return dict(row) if row else None


def target_for_user(user: UserState, mission_id: int) -> tuple[float, float, str]:
    dispatch = active_dispatch_for_user(user.user_id, mission_id)
    if dispatch is None:
        return PLS_LAT, PLS_LON, "staging"
    if dispatch["segment_id"] is None:
        return PLS_LAT, PLS_LON, "recall"
    if dispatch["entry_lat"] is not None and dispatch["entry_lon"] is not None:
        return float(dispatch["entry_lat"]), float(dispatch["entry_lon"]), dispatch["segment_name"]
    return float(dispatch["segment_lat"]), float(dispatch["segment_lon"]), dispatch["segment_name"]


def auto_ack_start_new_dispatches(client, users: dict[str, UserState], mission_id: int) -> list[dict[str, Any]]:
    """Pretend phones receive new orders quickly and teams start moving."""
    results: list[dict[str, Any]] = []
    for user in users.values():
        active = active_dispatch_for_user(user.user_id, mission_id)
        if not active:
            continue
        if active["status"] == "pending":
            ack = dispatch_action(client, user.token, int(active["id"]), "ack")
            results.append({"callsign": user.callsign, "dispatch_id": active["id"], "action": "ack", "status": ack["status"]})
            if active["segment_id"] is not None:
                start = dispatch_action(client, user.token, int(active["id"]), "start")
                results.append({"callsign": user.callsign, "dispatch_id": active["id"], "action": "start", "status": start["status"]})
    return results


def simulate_one_minute_pings(
    client,
    users: dict[str, UserState],
    mission_id: int,
    minute: int,
    ping_interval_seconds: int,
) -> dict[str, int]:
    steps = int(60 / ping_interval_seconds)
    counts: dict[str, int] = {}
    base_ts = int(time.time()) - 60
    for user in users.values():
        target_lat, target_lon, _target_label = target_for_user(user, mission_id)
        counts[user.callsign] = 0
        for step in range(steps):
            # Move partway toward the active target each simulated minute. Teams
            # rarely jump straight to a centroid; they close gradually.
            t = (step + 1) / steps
            close_fraction = 0.18 + (0.12 * t)
            lat = lerp(user.lat, target_lat, close_fraction)
            lon = lerp(user.lon, target_lon, close_fraction)
            dlat, dlon = tiny_detour(user.callsign, minute, step)
            lat = clamp(lat + dlat, PLS_LAT - HALF_LAT + 0.0002, PLS_LAT + HALF_LAT - 0.0002)
            lon = clamp(lon + dlon, PLS_LON - HALF_LON + 0.0002, PLS_LON + HALF_LON - 0.0002)
            distance = haversine_m(user.lat, user.lon, lat, lon)
            speed = min(1.8, max(0.1, distance / max(ping_interval_seconds, 1)))
            battery = max(35, 98 - minute * 2 - (step // 6))
            post_ping(
                client,
                user.token,
                lat,
                lon,
                base_ts + step * ping_interval_seconds,
                battery,
                speed,
            )
            user.lat = lat
            user.lon = lon
            counts[user.callsign] += 1
    return counts


def snapshot_db(mission_id: int) -> dict[str, Any]:
    from api.db import session

    with session() as conn:
        counts = dict(conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM pings WHERE mission_id = ?) AS pings,
              (SELECT COUNT(*) FROM findings WHERE mission_id = ?) AS findings,
              (SELECT COUNT(*) FROM dispatches WHERE mission_id = ?) AS dispatches,
              (SELECT COUNT(*) FROM broadcasts WHERE mission_id = ?) AS broadcasts,
              (SELECT COUNT(*) FROM hex_cells WHERE mission_id = ? AND flag_searched = 1) AS searched_hexes
            """,
            (mission_id, mission_id, mission_id, mission_id, mission_id),
        ).fetchone())
        active = [dict(row) for row in conn.execute(
            """
            SELECT u.callsign, u.status AS user_status, d.id AS dispatch_id,
                   d.status AS dispatch_status, s.name AS segment_name,
                   d.sweep_type, d.instruction
            FROM users u
            LEFT JOIN dispatches d ON d.id = (
              SELECT id FROM dispatches
              WHERE user_id = u.id AND mission_id = ?
                AND status IN ('pending', 'acked', 'in_progress')
              ORDER BY issued_ts DESC
              LIMIT 1
            )
            LEFT JOIN segments s ON s.id = d.segment_id
            WHERE u.current_mission_id = ?
            ORDER BY u.callsign
            """,
            (mission_id, mission_id),
        ).fetchall()]
        new_dispatches = [dict(row) for row in conn.execute(
            """
            SELECT d.id, u.callsign, d.segment_id, s.name AS segment_name,
                   d.sweep_type, d.status, d.instruction, d.reasoning,
                   d.superseded_by
            FROM dispatches d
            JOIN users u ON u.id = d.user_id
            LEFT JOIN segments s ON s.id = d.segment_id
            WHERE d.mission_id = ?
            ORDER BY d.id
            """,
            (mission_id,),
        ).fetchall()]
        recent_broadcasts = [dict(row) for row in conn.execute(
            """
            SELECT id, scope, kind, message, ts
            FROM broadcasts
            WHERE mission_id = ?
            ORDER BY id DESC
            LIMIT 8
            """,
            (mission_id,),
        ).fetchall()]
        top_segments = [dict(row) for row in conn.execute(
            """
            SELECT s.id, s.name, s.poa, s.pod, s.status,
                   u.callsign AS assigned_callsign,
                   s.sweep_type, s.target_pod,
                   (s.poa * (1.0 - s.pod)) AS remaining_probability,
                   s.dominant_cover, s.avg_slope_deg
            FROM segments s
            LEFT JOIN users u ON u.id = s.assigned_user_id
            WHERE s.mission_id = ?
            ORDER BY remaining_probability DESC
            LIMIT 8
            """,
            (mission_id,),
        ).fetchall()]
    return {
        "counts": counts,
        "active_assignments": active,
        "dispatches": new_dispatches,
        "recent_broadcasts": list(reversed(recent_broadcasts)),
        "top_segments": top_segments,
    }


def extract_openclaw_text(stdout: str) -> tuple[str | None, dict[str, Any] | None]:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end <= start:
        return None, None
    try:
        payload = json.loads(stdout[start:end + 1])
    except json.JSONDecodeError:
        return None, None
    texts = []
    for item in payload.get("result", {}).get("payloads", []):
        text = item.get("text")
        if text:
            texts.append(text)
    return "\n\n".join(texts), payload


def find_openclaw_container() -> str:
    out = subprocess.check_output(
        ["docker", "ps", "--format", "{{.ID}} {{.Names}}"],
        text=True,
    )
    for line_out in out.splitlines():
        if "openshell-my-assistant" in line_out or "openclaw" in line_out or "sandbox" in line_out:
            return line_out.split()[0]
    raise RuntimeError("Could not find running OpenClaw/OpenShell sandbox container")


def install_openclaw_runner(container_id: str) -> None:
    host_runner = Path("/tmp/geo_beacon_openclaw_stdin_runner.py")
    host_runner.write_text(OPENCLAW_STDIN_RUNNER, encoding="utf-8")
    subprocess.run(
        ["docker", "cp", str(host_runner), f"{container_id}:/tmp/geo_beacon_openclaw_stdin_runner.py"],
        check=True,
    )


def run_openclaw_turn(
    container_id: str,
    prompt: str,
    session_id: str,
    thinking: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["GB_OPENCLAW_SESSION_ID"] = session_id
    env["GB_OPENCLAW_THINKING"] = thinking
    env["GB_OPENCLAW_TIMEOUT"] = str(timeout_seconds)
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-u",
            "sandbox",
            "--env",
            f"GB_OPENCLAW_SESSION_ID={session_id}",
            "--env",
            f"GB_OPENCLAW_THINKING={thinking}",
            "--env",
            f"GB_OPENCLAW_TIMEOUT={timeout_seconds}",
            container_id,
            "python3",
            "/tmp/geo_beacon_openclaw_stdin_runner.py",
        ],
        input=prompt,
        text=True,
        capture_output=True,
        timeout=timeout_seconds + 90,
        check=False,
    )
    text, payload = extract_openclaw_text(proc.stdout)
    tool_summary = (
        payload
        and payload.get("result", {}).get("meta", {}).get("toolSummary")
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "assistant_text": text,
        "tool_summary": tool_summary,
        "payload": payload,
    }


def start_mcp_server(db_path: Path, log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["MISSION_DB_PATH"] = str(db_path)
    env["TERRAIN_MOCK"] = "1"
    env["GEO_BEACON_MCP_TRANSPORT"] = "streamable-http"
    env["GEO_BEACON_MCP_HOST"] = DEFAULT_MCP_HOST
    env["GEO_BEACON_MCP_PORT"] = str(DEFAULT_MCP_PORT)
    if DEFAULT_SPATIALITE.exists():
        env["SPATIALITE_PATH"] = str(DEFAULT_SPATIALITE)
        env["LD_LIBRARY_PATH"] = f"{DEFAULT_SPATIALITE.parent}:{env.get('LD_LIBRARY_PATH', '')}"
    subprocess.run(["tmux", "kill-session", "-t", "geo-beacon-mcp"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(REPO_ROOT / "scripts/run_agent_mcp_http.sh")],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(2)
    if proc.poll() is not None:
        raise RuntimeError(f"MCP server exited early; see {log_path}")
    return proc


def create_static_mission(client) -> tuple[int, str, dict[str, UserState], dict[str, dict[str, Any]]]:
    from api.db import session
    from api.db.hazards import bulk_insert_hazards
    from api.db.hex_cells import rasterize_hazard_to_hex_flags
    from api.db.osm import bulk_insert_osm_features
    from agent.skills.write import broadcast, dispatch_searcher

    created = client.post(
        "/missions",
        json={
            "name": "Redwood Gulch Dynamic Minute SAR",
            "subject_description": (
                "Missing 72-year-old mushroom forager, yellow poncho, wicker basket, "
                "insulin dependent, last seen leaving the old logging spur toward Redwood Gulch."
            ),
            "pls_lat": PLS_LAT,
            "pls_lon": PLS_LON,
            "pls_ts": int(time.time()) - 2 * 3600 - 20 * 60,
            "area_geojson": area_polygon(),
            "display_name": "Kilo Ridge Team",
            "callsign": "KILO",
        },
    )
    if created.status_code != 201:
        raise RuntimeError(f"mission create failed: {created.status_code} {created.text}")

    mission = created.json()
    mission_id = int(mission["mission_id"])
    join_code = mission["join_code"]
    users: dict[str, UserState] = {
        "KILO": UserState("KILO", mission["user_id"], mission["bearer_token"], PLS_LAT, PLS_LON),
    }
    for callsign, name in [
        ("LIMA", "Lima North Loop Team"),
        ("MIKE", "Mike Service Road Team"),
        ("NOVEMBER", "November Creek Team"),
        ("OSCAR", "Oscar Staging Team"),
    ]:
        joined = client.post(
            "/missions/join",
            json={
                "join_code": join_code,
                "display_name": name,
                "callsign": callsign,
                "role": "searcher",
            },
        )
        if joined.status_code != 201:
            raise RuntimeError(f"{callsign} join failed: {joined.status_code} {joined.text}")
        data = joined.json()
        users[callsign] = UserState(callsign, data["user_id"], data["bearer_token"], PLS_LAT, PLS_LON)

    segments = db_segment_rows(mission_id)
    targets = [
        ("ravine_floor", (0.27, 0.75)),
        ("mushroom_slope", (0.36, 0.62)),
        ("old_cabin", (0.48, 0.54)),
        ("north_loop", (0.82, 0.35)),
        ("south_road", (0.18, 0.26)),
        ("creek_crossing", (0.43, 0.84)),
        ("ridge_cut", (0.61, 0.18)),
        ("staging", (0.49, 0.48)),
        ("slide_slope", (0.31, 0.86)),
    ]
    chosen = choose_unique_segments(segments, targets)

    bulk_insert_osm_features(mission_id, [
        {
            "kind": "trail",
            "name": "Redwood Gulch Main Trail",
            "geom_geojson": line([
                (PLS_LAT, PLS_LON),
                (chosen["old_cabin"]["lat"], chosen["old_cabin"]["lon"]),
                (chosen["mushroom_slope"]["lat"], chosen["mushroom_slope"]["lon"]),
                (chosen["ravine_floor"]["lat"], chosen["ravine_floor"]["lon"]),
            ]),
        },
        {
            "kind": "trail",
            "name": "North Loop Trail",
            "geom_geojson": line([
                (PLS_LAT, PLS_LON),
                (chosen["north_loop"]["lat"], chosen["north_loop"]["lon"]),
                (chosen["ridge_cut"]["lat"], chosen["ridge_cut"]["lon"]),
            ]),
        },
        {
            "kind": "road",
            "name": "Old South Logging Road",
            "geom_geojson": line([
                (PLS_LAT - 0.0022, PLS_LON - HALF_LON * 0.85),
                (chosen["south_road"]["lat"], chosen["south_road"]["lon"]),
                (PLS_LAT - 0.0020, PLS_LON + HALF_LON * 0.80),
            ]),
        },
        {
            "kind": "trail",
            "name": "Creek Cutover",
            "geom_geojson": line([
                (chosen["old_cabin"]["lat"], chosen["old_cabin"]["lon"]),
                (chosen["creek_crossing"]["lat"], chosen["creek_crossing"]["lon"]),
                (chosen["slide_slope"]["lat"], chosen["slide_slope"]["lon"]),
            ]),
        },
    ])

    hazards = [
        {
            "kind": "water",
            "severity": "caution",
            "description": "Redwood Creek crossing is slippery and rising after drizzle.",
            "poly_geojson": box_poly(chosen["creek_crossing"]["lat"], chosen["creek_crossing"]["lon"], 0.0010, 0.0013),
        },
        {
            "kind": "cliff",
            "severity": "critical",
            "description": "Unstable ravine wall and wet leaf duff above the ravine floor.",
            "poly_geojson": box_poly(chosen["slide_slope"]["lat"], chosen["slide_slope"]["lon"], 0.0012, 0.0010),
        },
        {
            "kind": "no_comms_zone",
            "severity": "caution",
            "description": "Radio shadow in lower Redwood Gulch below mushroom slope.",
            "poly_geojson": box_poly(chosen["ravine_floor"]["lat"], chosen["ravine_floor"]["lon"], 0.0014, 0.0014),
        },
        {
            "kind": "weather",
            "severity": "caution",
            "description": "Cold drizzle and dusk reduce visibility under redwood canopy.",
            "poly_geojson": box_poly(chosen["mushroom_slope"]["lat"], chosen["mushroom_slope"]["lon"], 0.0014, 0.0015),
        },
    ]
    hazard_ids = bulk_insert_hazards(mission_id, hazards)
    for hazard_id in hazard_ids:
        rasterize_hazard_to_hex_flags(mission_id, hazard_id)

    role_weights = {
        "ravine_floor": 0.18,
        "mushroom_slope": 0.145,
        "old_cabin": 0.105,
        "slide_slope": 0.085,
        "creek_crossing": 0.07,
        "ridge_cut": 0.045,
        "north_loop": 0.03,
        "south_road": 0.018,
        "staging": 0.012,
    }
    weighted_ids = {int(chosen[label]["id"]) for label in role_weights}
    remaining = 1.0 - sum(role_weights.values())
    base = remaining / max(1, len(segments) - len(weighted_ids))
    with session() as conn:
        conn.execute("BEGIN")
        for seg in segments:
            poa = base
            for label, weight in role_weights.items():
                if int(chosen[label]["id"]) == int(seg["id"]):
                    poa = weight
                    break
            conn.execute(
                """
                UPDATE segments
                SET poa = ?, pod = 0, pos = 0, status = 'unassigned',
                    assigned_user_id = NULL, sweep_type = NULL, target_pod = NULL
                WHERE id = ? AND mission_id = ?
                """,
                (poa, seg["id"], mission_id),
            )
        conn.execute("COMMIT")

    initial_dispatches = {
        "KILO": ("old_cabin", "hasty", "Search old cabin spur and nearby mushroom pullouts."),
        "LIMA": ("north_loop", "efficient", "Sweep north loop trail and check drainage crossings."),
        "MIKE": ("south_road", "efficient", "Clear old south logging road and cable turnouts."),
        "NOVEMBER": ("creek_crossing", "hasty", "Check creek crossing; stay above high water and report hazards."),
    }
    for callsign, (label, sweep_type, instruction) in initial_dispatches.items():
        seg = chosen[label]
        dispatch_searcher(
            user_id=users[callsign].user_id,
            segment_id=int(seg["id"]),
            sweep_type=sweep_type,
            instruction=instruction,
            reasoning=(
                f"Realtime scenario setup: {callsign} starts in {seg['name']} "
                "before minute-by-minute evidence arrives."
            ),
            mission_id=mission_id,
        )

    broadcast(
        scope="all",
        kind="warning",
        message="Scenario start: wet duff and dusk under canopy; avoid direct ravine descent.",
        reasoning="Initial safety context for realtime simulation.",
        mission_id=mission_id,
    )

    return mission_id, join_code, users, chosen


def apply_scheduled_events(
    client,
    users: dict[str, UserState],
    chosen: dict[str, dict[str, Any]],
    minute: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def add_finding(callsign: str, label: str, kind: str, desc: str, conf: float) -> None:
        seg = chosen[label]
        fid = log_finding(client, users[callsign].token, float(seg["lat"]), float(seg["lon"]), kind, desc, conf)
        events.append({
            "type": "finding",
            "finding_id": fid,
            "callsign": callsign,
            "segment": seg["name"],
            "kind": kind,
            "confidence": conf,
        })

    if minute == 1:
        add_finding(
            "KILO",
            "mushroom_slope",
            "clue",
            "Freshly cut chanterelle stems and torn wicker basket fiber leading downslope.",
            0.82,
        )
    elif minute == 2:
        add_finding(
            "LIMA",
            "north_loop",
            "clue",
            "North loop has only old boot tracks and no basket fibers; likely a low-value branch.",
            0.69,
        )
    elif minute == 3:
        add_finding(
            "MIKE",
            "south_road",
            "clue",
            "South logging road shows vehicle tracks and dog-walker prints, no matching subject sign.",
            0.66,
        )
    elif minute == 4:
        add_finding(
            "NOVEMBER",
            "creek_crossing",
            "hazard",
            "Creek stepping stones are slick; water has risen enough to slow crossing.",
            0.78,
        )
    elif minute == 5:
        add_finding(
            "KILO",
            "ravine_floor",
            "discarded_item",
            "Wicker mushroom basket found overturned near ravine-floor fern patch.",
            0.88,
        )
    elif minute == 6:
        add_finding(
            "NOVEMBER",
            "slide_slope",
            "subject_sighting",
            "Two weak voice calls heard from below slide slope toward ravine floor.",
            0.61,
        )
    elif minute == 8:
        add_finding(
            "OSCAR",
            "ravine_floor",
            "subject_found",
            "Subject located conscious but cold near ravine-floor log, requesting medical help.",
            0.97,
        )

    return events


def build_turn_prompt(prompt_path: Path, mission_id: int, minute: int, total_minutes: int) -> str:
    from agent.brief import compose_brief

    standing_prompt = prompt_path.read_text(encoding="utf-8").strip()
    brief = compose_brief(mission_id=mission_id)
    return (
        f"{standing_prompt}\n\n"
        f"----- CURRENT MISSION BRIEF - SIMULATED MINUTE {minute}/{total_minutes} -----\n"
        f"{brief.strip()}\n"
        "----- END BRIEF -----\n\n"
        "You are being called by the one-minute worker loop. Treat this as one minute of live mission time.\n"
        "Do not assume future findings exist. Act only on the database state in this brief and tool reads.\n"
        "Avoid repeating previous dispatches or broadcasts unless the situation changed.\n"
        "Make at most two high-confidence operational writes this turn, unless subject_found requires mission closeout.\n"
        "If subject_found appears, update mission status, broadcast all-hands, and recall/redirect teams as appropriate.\n"
        "End with a concise summary of what you did or why you did nothing.\n"
    )


def run(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    db_path = Path(args.db_path) if args.db_path else REPO_ROOT / "dev" / "data" / f"realtime_openclaw_{int(time.time())}.db"
    configure_env(db_path)

    from fastapi.testclient import TestClient
    from api.main import app

    output_dir = REPO_ROOT / "dev" / "data" / "realtime_runs"
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"redwood_gulch_{int(time.time())}"
    report_path = output_dir / f"{run_id}.jsonl"
    mcp_log_path = output_dir / f"{run_id}_mcp.log"

    log("realtime_sim_start", run_id=run_id, db_path=str(db_path), report_path=str(report_path))
    mcp_proc: subprocess.Popen | None = None
    container_id: str | None = None

    with TestClient(app) as client, report_path.open("w", encoding="utf-8") as report:
        mission_id, join_code, users, chosen = create_static_mission(client)
        acked = auto_ack_start_new_dispatches(client, users, mission_id)
        setup_snapshot = snapshot_db(mission_id)
        setup_record = {
            "event": "setup_complete",
            "run_id": run_id,
            "db_path": str(db_path),
            "mission_id": mission_id,
            "join_code": join_code,
            "users": {k: {"user_id": v.user_id} for k, v in users.items()},
            "chosen_segments": {
                label: {
                    "id": seg["id"],
                    "name": seg["name"],
                    "lat": seg["lat"],
                    "lon": seg["lon"],
                    "poa": seg["poa"],
                    "dominant_cover": seg["dominant_cover"],
                    "avg_slope_deg": seg["avg_slope_deg"],
                }
                for label, seg in chosen.items()
            },
            "auto_ack_start": acked,
            "snapshot": setup_snapshot,
        }
        report.write(json.dumps(setup_record, sort_keys=True) + "\n")
        report.flush()
        log("setup_complete", mission_id=mission_id, segments=378, users=list(users))

        if args.run_openclaw:
            mcp_proc = start_mcp_server(db_path, mcp_log_path)
            container_id = find_openclaw_container()
            install_openclaw_runner(container_id)
            log("openclaw_ready", container_id=container_id, mcp_log=str(mcp_log_path))

        try:
            for minute in range(1, args.minutes + 1):
                ping_counts = simulate_one_minute_pings(
                    client,
                    users,
                    mission_id,
                    minute,
                    args.ping_interval_seconds,
                )
                scheduled_events = apply_scheduled_events(client, users, chosen, minute)
                before_turn = snapshot_db(mission_id)
                record: dict[str, Any] = {
                    "event": "minute_before_agent",
                    "minute": minute,
                    "ping_counts_this_minute": ping_counts,
                    "scheduled_events": scheduled_events,
                    "snapshot": before_turn,
                }
                report.write(json.dumps(record, sort_keys=True) + "\n")
                report.flush()
                log(
                    "minute_before_agent",
                    minute=minute,
                    pings=before_turn["counts"]["pings"],
                    findings=before_turn["counts"]["findings"],
                    dispatches=before_turn["counts"]["dispatches"],
                    events=len(scheduled_events),
                )

                openclaw_result: dict[str, Any] | None = None
                if args.run_openclaw:
                    assert container_id is not None
                    prompt = build_turn_prompt(args.prompt, mission_id, minute, args.minutes)
                    session_id = f"{args.session_prefix}-{run_id}-m{minute:02d}"
                    started = time.time()
                    openclaw_result = run_openclaw_turn(
                        container_id,
                        prompt,
                        session_id,
                        args.thinking,
                        args.openclaw_timeout_seconds,
                    )
                    duration_s = round(time.time() - started, 2)
                    log(
                        "openclaw_turn_complete",
                        minute=minute,
                        returncode=openclaw_result["returncode"],
                        duration_s=duration_s,
                        tool_summary=openclaw_result.get("tool_summary"),
                    )
                else:
                    openclaw_result = {
                        "returncode": 0,
                        "assistant_text": None,
                        "tool_summary": None,
                        "stdout": "",
                        "stderr": "",
                    }

                after_turn = snapshot_db(mission_id)
                acked_after_turn = auto_ack_start_new_dispatches(client, users, mission_id)
                after_ack = snapshot_db(mission_id)
                result_record = {
                    "event": "minute_after_agent",
                    "minute": minute,
                    "openclaw": {
                        "returncode": openclaw_result["returncode"],
                        "assistant_text": openclaw_result.get("assistant_text"),
                        "tool_summary": openclaw_result.get("tool_summary"),
                        "stderr_tail": (openclaw_result.get("stderr") or "")[-2000:],
                    },
                    "snapshot_after_agent": after_turn,
                    "auto_ack_start": acked_after_turn,
                    "snapshot_after_ack": after_ack,
                }
                report.write(json.dumps(result_record, sort_keys=True) + "\n")
                report.flush()
                log(
                    "minute_after_agent",
                    minute=minute,
                    dispatches=after_ack["counts"]["dispatches"],
                    broadcasts=after_ack["counts"]["broadcasts"],
                    acked=len(acked_after_turn),
                )

        finally:
            if mcp_proc is not None:
                mcp_proc.send_signal(signal.SIGTERM)
                try:
                    mcp_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    mcp_proc.kill()

    log("realtime_sim_complete", run_id=run_id, db_path=str(db_path), report_path=str(report_path))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--ping-interval-seconds", type=int, default=5)
    parser.add_argument("--db-path", default="")
    parser.add_argument("--run-openclaw", action="store_true", default=True)
    parser.add_argument("--no-openclaw", dest="run_openclaw", action="store_false")
    parser.add_argument("--thinking", default="off", choices=["off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"])
    parser.add_argument("--openclaw-timeout-seconds", type=int, default=900)
    parser.add_argument("--session-prefix", default="realtime-sar")
    parser.add_argument("--prompt", type=Path, default=REPO_ROOT / "openclaw" / "agent_prompt.md")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
