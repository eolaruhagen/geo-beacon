"""DB helpers for the dispatches table.

A dispatch is one agent-issued order to a single searcher: "go sweep segment
S-r03-c07 with sweep_type='efficient', entry at (lat, lon)." The dispatch
lifecycle is:

    pending → acked → in_progress → completed     (normal path)
                                  → cancelled     (mission ended, etc.)
                                  → superseded    (agent reassigned the user)

Transitions are gated at the route layer; this module just exposes loads,
the current-active lookup, and the transition write.
"""
from __future__ import annotations

import json
import time

from api.db import session

ACTIVE_STATUSES = ("pending", "acked", "in_progress")

_BASE_COLUMNS = (
    "id, mission_id, user_id, segment_id, sweep_type, entry_lat, entry_lon, "
    "instruction, reasoning, status, issued_ts, acked_ts, started_ts, "
    "completed_ts, completion_notes, superseded_by"
)


def get_dispatch(dispatch_id: int) -> dict | None:
    """Load full dispatch row by id, or None."""
    with session() as conn:
        row = conn.execute(
            f"SELECT {_BASE_COLUMNS} FROM dispatches WHERE id = ?",
            (dispatch_id,),
        ).fetchone()
        return dict(row) if row else None


def active_dispatch_for_user(user_id: int) -> dict | None:
    """Most-recently-issued dispatch for the user in an active status, or None.

    A searcher only has one in-flight dispatch at a time in practice (the
    agent supersedes the previous one when reassigning), but we order by
    issued_ts DESC defensively so the newest active row wins if both exist.
    """
    with session() as conn:
        row = conn.execute(
            f"""
            SELECT {_BASE_COLUMNS} FROM dispatches
            WHERE user_id = ?
              AND status IN ('pending', 'acked', 'in_progress')
            ORDER BY issued_ts DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


_ALLOWED_TS_FIELDS = {"acked_ts", "started_ts", "completed_ts"}


def transition_status(
    dispatch_id: int,
    new_status: str,
    ts_field: str | None = None,
    completion_notes: str | None = None,
) -> None:
    """Move a dispatch to `new_status` and stamp `ts_field` with now() in one
    UPDATE. Caller is responsible for validating the previous status before
    calling — this writes unconditionally.

    `completion_notes` is only persisted when new_status == 'completed'.
    """
    now = int(time.time())
    fields = ["status = ?"]
    params: list = [new_status]

    if ts_field is not None:
        if ts_field not in _ALLOWED_TS_FIELDS:
            raise ValueError(f"unsupported ts_field {ts_field!r}")
        fields.append(f"{ts_field} = ?")
        params.append(now)

    if new_status == "completed" and completion_notes is not None:
        fields.append("completion_notes = ?")
        params.append(completion_notes)

    params.append(dispatch_id)
    with session() as conn:
        conn.execute(
            f"UPDATE dispatches SET {', '.join(fields)} WHERE id = ?",
            params,
        )


def segment_feature_for_dispatch(dispatch: dict) -> dict | None:
    """Return the dispatch's segment as a GeoJSON Feature, or None for recalls
    (segment_id NULL) or if the segment row has been deleted.

    Shape mirrors the segments features served by /mission/state.geojson so
    the app can reuse rendering code.
    """
    seg_id = dispatch.get("segment_id")
    if seg_id is None:
        return None
    with session() as conn:
        row = conn.execute(
            """
            SELECT name, poa, pod, status, sweep_type, target_pod,
                   avg_slope_deg, dominant_cover, trail_length_m, area_m2,
                   AsGeoJSON(geom) AS gj
            FROM segments WHERE id = ?
            """,
            (seg_id,),
        ).fetchone()
    if row is None or row["gj"] is None:
        return None
    return {
        "type": "Feature",
        "geometry": json.loads(row["gj"]),
        "properties": {
            "segment_id": seg_id,
            "name": row["name"],
            "poa": row["poa"],
            "pod": row["pod"],
            "status": row["status"],
            "sweep_type": row["sweep_type"],
            "target_pod": row["target_pod"],
            "avg_slope_deg": row["avg_slope_deg"],
            "dominant_cover": row["dominant_cover"],
            "trail_length_m": row["trail_length_m"],
            "area_m2": row["area_m2"],
        },
    }
