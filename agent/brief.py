"""Mission brief composer for the OpenClaw agent turn."""
from __future__ import annotations

import time

from agent.skills.read import (
    get_findings,
    get_mission_overview,
    list_searchers,
)
from api.db import session


# Turns a timestamp into a small clock string.
# If the time is missing, it says unknown instead of crashing.
def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "unknown"
    return time.strftime("%H:%M:%S", time.localtime(ts))


# Turns an old timestamp into "how many minutes ago."
# This helps the brief say whether a ping or sighting is fresh.
def _age_min(ts: int | None, now: int) -> str:
    if ts is None:
        return "unknown"
    return f"{max(0, int((now - ts) / 60))} min"


# Finds hazards that are still active for this mission.
# It also names the segments those hazards touch.
def _active_hazards(mission_id: int) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            """
            SELECT h.id, h.kind, h.severity, h.description,
                   GROUP_CONCAT(s.name, ', ') AS segment_names
            FROM hazards h
            LEFT JOIN segments s
              ON s.mission_id = h.mission_id
             AND ST_Intersects(s.geom, h.geom)
            WHERE h.mission_id = ?
              AND (h.expires_ts IS NULL OR h.expires_ts > ?)
            GROUP BY h.id
            ORDER BY h.severity DESC, h.created_ts DESC
            LIMIT 8
            """,
            (mission_id, int(time.time())),
        ).fetchall()
    return [dict(row) for row in rows]


# Pulls recent dispatches and broadcasts into one short list.
# This lets the brief remind the agent what it recently told people to do.
def _recent_actions(mission_id: int, since_ts: int) -> list[dict]:
    actions: list[dict] = []
    with session() as conn:
        dispatches = conn.execute(
            """
            SELECT d.issued_ts AS ts, 'dispatch' AS type, u.callsign,
                   d.segment_id, s.name AS segment_name, d.status,
                   d.instruction, d.reasoning
            FROM dispatches d
            JOIN users u ON u.id = d.user_id
            LEFT JOIN segments s ON s.id = d.segment_id
            WHERE d.mission_id = ? AND d.issued_ts >= ?
            ORDER BY d.issued_ts DESC
            LIMIT 8
            """,
            (mission_id, since_ts),
        ).fetchall()
        broadcasts = conn.execute(
            """
            SELECT ts, 'broadcast' AS type, scope, kind, message
            FROM broadcasts
            WHERE mission_id = ? AND ts >= ?
            ORDER BY ts DESC
            LIMIT 8
            """,
            (mission_id, since_ts),
        ).fetchall()
    actions.extend(dict(row) for row in dispatches)
    actions.extend(dict(row) for row in broadcasts)
    actions.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return actions[:8]


# Builds the markdown mission brief for the agent.
# It is the clean "story so far" before OpenClaw decides what tools to call.
def compose_brief(
    mission_id: int | None = None,
    now_ts: int | None = None,
    ascii_map: str | None = None,
) -> str:
    """Build deterministic markdown context for an OpenClaw invocation."""
    now = now_ts or int(time.time())
    overview = get_mission_overview(mission_id)
    mid = int(overview["id"])
    since_30m = now - 1800

    lines: list[str] = []
    lines.append(f"# Mission Brief - {overview['name']} - {_fmt_ts(now)}")
    lines.append("")
    lines.append("## Mission Status")
    lines.append(f"- Mission ID: {mid}")
    lines.append(f"- Subject: {overview['subject_description']}")
    lines.append(
        f"- PLS: {overview['pls_lat']:.6f}, {overview['pls_lon']:.6f} "
        f"at {_fmt_ts(overview['pls_ts'])} ({_age_min(overview['pls_ts'], now)} ago)"
    )
    lines.append(f"- Status: {overview['status']}")
    lines.append(
        f"- Active searchers: {overview['active_searchers']}/{overview['total_searchers']}"
    )

    lines.append("")
    lines.append("## Coverage Summary")
    lines.append(
        f"- Segments swept/cleared: {overview['swept_segments']}/{overview['total_segments']}"
    )

    if ascii_map:
        lines.append("")
        lines.append("## Current Map")
        lines.append("```text")
        lines.append(ascii_map.strip())
        lines.append("```")

    searchers = list_searchers(mid)
    if searchers:
        lines.append("")
        lines.append("## Searchers")
        for s in searchers:
            call = s["callsign"] or s["display_name"]
            ping = s["latest_ping"]
            ping_age = _age_min(ping["ts"], now) if ping else "no ping"
            dispatch = s["active_dispatch"]
            if dispatch:
                assignment = (
                    f"on {dispatch['segment_name']} status={dispatch['status']}, "
                    f"sweep={dispatch['sweep_type']}"
                )
            else:
                assignment = "no active dispatch"
            lines.append(f"- {call}: status={s['status']}, {assignment}, last ping={ping_age}")

    findings = get_findings(since_ts=since_30m, mission_id=mid, limit=8)
    if findings:
        lines.append("")
        lines.append("## Recent Findings")
        for f in findings:
            desc = f["description"] or ""
            segment = f["segment_name"] or f"hex {f['hex_id']}"
            lines.append(
                f"- {_fmt_ts(f['ts'])}: {f['kind']} at {segment}: {desc!r}"
            )

    hazards = _active_hazards(mid)
    if hazards:
        lines.append("")
        lines.append("## Active Hazards")
        for h in hazards:
            segments = h["segment_names"] or "no segment intersection"
            lines.append(
                f"- {h['kind']} ({h['severity']}, affects {segments}): {h['description']}"
            )

    actions = _recent_actions(mid, since_30m)
    if actions:
        lines.append("")
        lines.append("## Recent Agent Actions")
        for a in actions:
            if a["type"] == "dispatch":
                lines.append(
                    f"- {_fmt_ts(a['ts'])}: dispatch to {a['callsign']} -> "
                    f"{a['segment_name'] or 'recall'} ({a['status']}): {a['instruction']}"
                )
            else:
                lines.append(
                    f"- {_fmt_ts(a['ts'])}: broadcast {a['kind']} to {a['scope']}: {a['message']}"
                )

    lines.append("")
    lines.append("## Open Questions")
    stale = []
    for s in searchers:
        ping = s["latest_ping"]
        if ping is None or now - int(ping["ts"]) > 600:
            stale.append(s["callsign"] or s["display_name"])
    if stale:
        lines.append(f"- No recent comms from: {', '.join(stale)}")
    lines.append("- Decide whether to dispatch idle searchers, reassign active searches, recall teams, or broadcast safety updates.")

    return "\n".join(lines).strip() + "\n"
