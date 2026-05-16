"""Field routes: ping ingestion, searcher state, and findings."""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import current_user
from api.db import session
from api.db.hazards import bulk_insert_hazards
from api.db.hex_cells import (
    hex_cell_id_at,
    hex_cells_for_mission,
    mark_hex_searched,
    mark_segment_searched,
    rasterize_hazard_to_hex_flags,
    set_flag_clue_for_hex,
)
from api.schemas import (
    ActiveDispatch,
    AnnouncementsResponse,
    Broadcast,
    DispatchActionResponse,
    DispatchCompleteRequest,
    FindingRequest,
    FindingResponse,
    MeResponse,
    PingRequest,
    PingResponse,
    RouteResponse,
    RouteWaypoint,
    UserPublic,
)
import api.db.broadcasts as db_broadcasts
import api.db.dispatches as db_dispatches
import api.db.missions as db_missions
import api.db.pings as db_pings
import api.db.users as db_users
from api.db.routing import snap_point_to_nearest_trail


# Cap on inline broadcasts surfaced by GET /field/me. The full history is
# available via /field/announcements with watermark pagination, so this only
# needs to cover "what banner should the app show right now" — last few alerts.
ME_BROADCASTS_LIMIT = 5

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/field", tags=["field"])


@router.post("/ping", response_model=PingResponse)
async def field_ping(
    body: PingRequest,
    user: dict = Depends(current_user),
) -> PingResponse:
    mission_id = db_missions.active_mission_id_for_user(user["id"])
    if mission_id is None:
        raise HTTPException(status_code=409, detail="No active mission for this user")

    ts = body.ts if body.ts is not None else int(time.time())
    ping_id = db_pings.insert_ping(
        user_id=user["id"],
        mission_id=mission_id,
        lat=body.lat,
        lon=body.lon,
        ts=ts,
        accuracy_m=body.accuracy_m,
        speed_mps=body.speed_mps,
        battery_pct=body.battery_pct,
        source="phone",
    )

    # Auto-mark-searched: tag the containing hex as covered. Best-effort —
    # a PIP or UPDATE failure here must NOT fail the ping itself.
    try:
        hex_id = hex_cell_id_at(mission_id, body.lat, body.lon)
        if hex_id is not None:
            mark_hex_searched(hex_id, user["id"], ts)
    except Exception as e:
        logger.error("hex coverage update failed for ping %s: %s", ping_id, e)

    return PingResponse(ping_id=ping_id)


@router.get("/me", response_model=MeResponse)
async def field_me(user: dict = Depends(current_user)) -> MeResponse:
    mission_id = db_missions.active_mission_id_for_user(user["id"])

    active = db_dispatches.active_dispatch_for_user(user["id"])
    active_model: ActiveDispatch | None = None
    seg_feature: dict | None = None
    if active is not None:
        active_model = ActiveDispatch.model_validate(active)
        seg_feature = db_dispatches.segment_feature_for_dispatch(active)

    # Visibility filter (scope='all' OR scope='user:{id}') is enforced inside
    # db_broadcasts.visible_broadcasts_for_user — see the module docstring.
    recent: list[Broadcast] = []
    if mission_id is not None:
        rows = db_broadcasts.visible_broadcasts_for_user(
            user_id=user["id"], mission_id=mission_id, limit=ME_BROADCASTS_LIMIT,
        )
        recent = [Broadcast.model_validate(r) for r in rows]

    return MeResponse(
        user=UserPublic.model_validate(user),
        mission_id=mission_id,
        active_dispatch=active_model,
        segment_geojson=seg_feature,
        recent_broadcasts=recent,
    )


@router.get("/announcements", response_model=AnnouncementsResponse)
async def field_announcements(
    since: int = Query(0, ge=0, description="Unix-epoch seconds; returns broadcasts with ts > since"),
    user: dict = Depends(current_user),
) -> AnnouncementsResponse:
    """Watermark-paginated broadcasts visible to this user.

    Visibility policy is enforced in db_broadcasts (scope='all' OR
    scope=f'user:{user.id}'). The app stores the returned `cursor_ts` and
    re-polls with `?since=cursor_ts` for incremental delivery.
    """
    mission_id = db_missions.active_mission_id_for_user(user["id"])
    if mission_id is None:
        raise HTTPException(status_code=409, detail="No active mission for this user")

    rows = db_broadcasts.visible_broadcasts_for_user(
        user_id=user["id"], mission_id=mission_id, since_ts=since,
    )
    broadcasts = [Broadcast.model_validate(r) for r in rows]
    cursor = max((b.ts for b in broadcasts), default=since)
    return AnnouncementsResponse(broadcasts=broadcasts, cursor_ts=cursor)


# Allowed state transitions: keys are required current statuses, values are
# the (new_status, ts_field, new_user_status_or_none) tuple. Kept here as a
# single source of truth so the three endpoints stay consistent.
_DISPATCH_TRANSITIONS: dict[str, tuple[str, str, str, str | None]] = {
    # action     → (required_current_status, new_status,    ts_field,       new_user_status)
    "ack":         ("pending",                "acked",       "acked_ts",     None),
    "start":       ("acked",                  "in_progress", "started_ts",   "on_segment"),
    "complete":    ("in_progress",            "completed",   "completed_ts", "standby"),
}


def _authorize_and_load_dispatch(dispatch_id: int, user: dict) -> dict:
    """Load the dispatch, 404 if missing, 403 if it belongs to a different user."""
    d = db_dispatches.get_dispatch(dispatch_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Dispatch not found")
    if d["user_id"] != user["id"]:
        # Don't reveal which user it belongs to.
        raise HTTPException(status_code=403, detail="Not your dispatch")
    return d


def _apply_dispatch_action(
    dispatch_id: int,
    action: str,
    user: dict,
    completion_notes: str | None = None,
) -> DispatchActionResponse:
    required_status, new_status, ts_field, new_user_status = _DISPATCH_TRANSITIONS[action]
    d = _authorize_and_load_dispatch(dispatch_id, user)

    if d["status"] != required_status:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot {action} dispatch in status {d['status']!r}; "
                f"this action requires status {required_status!r}."
            ),
        )

    db_dispatches.transition_status(
        dispatch_id, new_status, ts_field=ts_field,
        completion_notes=completion_notes,
    )

    user_status_after = user["status"]
    if new_user_status is not None:
        db_users.set_user_status(user["id"], new_user_status)
        user_status_after = new_user_status

    # On /complete: mark every hex cell in the dispatched segment as searched
    # by this user. Per spec, completing a dispatch means the searcher
    # certifies the whole segment is covered — the per-cell flag_searched
    # already gets set incrementally by /field/ping as they walk, but for
    # cells they didn't physically step into this fills in the gaps so the
    # UI coverage tint matches reality. No-op when segment_id is null
    # (recall dispatches, where there's no segment to mark).
    if action == "complete" and d.get("segment_id") is not None:
        try:
            mark_segment_searched(
                mission_id=d["mission_id"],
                segment_id=d["segment_id"],
                user_id=user["id"],
                ts=int(time.time()),
            )
        except Exception as e:
            # Coverage update is best-effort; don't fail the lifecycle action
            # because of a stray UPDATE error. The status flip is the
            # authoritative signal.
            logger.error(
                "mark_segment_searched failed for dispatch %s: %s",
                dispatch_id, e,
            )

    return DispatchActionResponse(
        dispatch_id=dispatch_id,
        status=new_status,
        user_status=user_status_after,
    )


@router.post("/dispatch/{dispatch_id}/ack", response_model=DispatchActionResponse)
async def dispatch_ack(
    dispatch_id: int,
    user: dict = Depends(current_user),
) -> DispatchActionResponse:
    return _apply_dispatch_action(dispatch_id, "ack", user)


@router.post("/dispatch/{dispatch_id}/start", response_model=DispatchActionResponse)
async def dispatch_start(
    dispatch_id: int,
    user: dict = Depends(current_user),
) -> DispatchActionResponse:
    return _apply_dispatch_action(dispatch_id, "start", user)


@router.post("/dispatch/{dispatch_id}/complete", response_model=DispatchActionResponse)
async def dispatch_complete(
    dispatch_id: int,
    body: DispatchCompleteRequest,
    user: dict = Depends(current_user),
) -> DispatchActionResponse:
    return _apply_dispatch_action(
        dispatch_id, "complete", user, completion_notes=body.notes,
    )


@router.get("/me/route", response_model=RouteResponse)
async def field_me_route(
    segment_id: int | None = Query(
        None,
        description=(
            "Optional target segment id. Omit for the user's active dispatch "
            "entry point, including cell-grain dispatches."
        ),
    ),
    user: dict = Depends(current_user),
) -> RouteResponse:
    """Snap-to-trail route from the user's last known position to the target.

    Resolution order for the start point: most recent ping → mission PLS.
    Resolution order for the target point: active dispatch entry_lat/lon,
    otherwise the provided segment centroid.
    """
    mission_id = db_missions.active_mission_id_for_user(user["id"])
    if mission_id is None:
        raise HTTPException(status_code=409, detail="No active mission for this user")

    # Start: latest ping → PLS fallback.
    latest = db_pings.latest_ping_for_user(user["id"], mission_id)
    if latest is not None:
        start_lat, start_lon = float(latest["lat"]), float(latest["lon"])
    else:
        mission = db_missions.get_mission(mission_id)
        # mission can't be None here — active_mission_id_for_user just returned it.
        start_lat, start_lon = float(mission["pls_lat"]), float(mission["pls_lon"])

    active = db_dispatches.active_dispatch_for_user(user["id"])

    seg = None
    if segment_id is not None:
        with session() as conn:
            seg = conn.execute(
                """
                SELECT id, X(Centroid(geom)) AS clon, Y(Centroid(geom)) AS clat
                FROM segments WHERE id = ? AND mission_id = ?
                """,
                (segment_id, mission_id),
            ).fetchone()
        if seg is None:
            raise HTTPException(
                status_code=404,
                detail=f"Segment {segment_id} not found in this mission",
            )

    # Target: prefer the active dispatch's entry point when it matches the
    # requested segment, or when no segment was requested. That covers routing
    # dispatches whose `segment_id` is NULL and whose target is just a point.
    if (
        active is not None
        and active.get("entry_lat") is not None
        and active.get("entry_lon") is not None
        and (segment_id is None or active.get("segment_id") == segment_id)
    ):
        target_lat = float(active["entry_lat"])
        target_lon = float(active["entry_lon"])
    elif seg is not None:
        target_lat = float(seg["clat"])
        target_lon = float(seg["clon"])
    else:
        raise HTTPException(status_code=409, detail="No active dispatch target to route to")

    snap_start = snap_point_to_nearest_trail(mission_id, start_lat, start_lon)
    snap_target = snap_point_to_nearest_trail(mission_id, target_lat, target_lon)

    waypoints: list[RouteWaypoint] = [RouteWaypoint(lat=start_lat, lon=start_lon)]
    snapped = False
    if snap_start is not None and snap_target is not None:
        waypoints.append(RouteWaypoint(lat=snap_start[0], lon=snap_start[1]))
        waypoints.append(RouteWaypoint(lat=snap_target[0], lon=snap_target[1]))
        snapped = True
    waypoints.append(RouteWaypoint(lat=target_lat, lon=target_lon))

    return RouteResponse(waypoints=waypoints, snapped=snapped)


@router.post("/findings", response_model=FindingResponse, status_code=201)
async def log_finding(
    body: FindingRequest,
    user: dict = Depends(current_user),
) -> FindingResponse:
    mission_id = db_missions.active_mission_id_for_user(user["id"])
    if mission_id is None:
        raise HTTPException(status_code=409, detail="No active mission for this user")

    hex_id: int | None = None
    lat = body.lat
    lon = body.lon

    # Resolve hex_id from either provided hex_id or lat/lon. Narrow except so a
    # genuine bug in the resolver doesn't get swallowed.
    if body.hex_id is not None:
        hex_id = body.hex_id
        if lat is None or lon is None:
            # resolve centroid from hex_id
            cells = hex_cells_for_mission(mission_id)
            for cell in cells:
                if cell["id"] == hex_id:
                    lat = cell.get("center_lat")
                    lon = cell.get("center_lon")
                    break
    else:
        # lat/lon guaranteed by model_validator
        hex_id = hex_cell_id_at(mission_id, lat, lon)  # type: ignore[arg-type]

    # hex_id is NOT NULL in the schema. If we couldn't resolve a containing
    # hex (point outside the mission grid), surface a clean 422 rather than
    # 500 on the NULL insert.
    if hex_id is None:
        raise HTTPException(
            status_code=422,
            detail="Point is outside any hex cell for the active mission",
        )
    if lat is None or lon is None:
        raise HTTPException(
            status_code=422,
            detail="Could not resolve lat/lon for the given hex_id",
        )

    set_flag_clue_for_hex(hex_id)

    ts = int(time.time())
    try:
        with session() as db:
            cur = db.execute(
                """
                INSERT INTO findings
                    (mission_id, reporter_user_id, hex_id, ts, lat, lon, kind, description, confidence, geom)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, SetSRID(MakePoint(?, ?), 4326))
                """,
                (
                    mission_id,
                    user["id"],
                    hex_id,
                    ts,
                    lat,
                    lon,
                    body.kind,
                    body.description,
                    body.confidence,
                    lon,
                    lat,
                ),
            )
            finding_id = cur.lastrowid
    except Exception as e:
        logger.error("insert finding failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to insert finding")

    # Hex-marking branch: when a searcher taps "hazard", drop a hazard polygon
    # matching the containing hex and rasterize its flag_danger onto the grid.
    # findings.kind='hazard' (generic) maps to hazards.kind='other' since the
    # two enums don't align 1:1.
    if body.kind == "hazard":
        try:
            with session() as db:
                row = db.execute(
                    "SELECT AsGeoJSON(geom) AS poly_geojson FROM hex_cells WHERE id = ?",
                    (hex_id,),
                ).fetchone()
            if row and row["poly_geojson"]:
                poly = json.loads(row["poly_geojson"])
                hazard_ids = bulk_insert_hazards(
                    mission_id,
                    [
                        {
                            "kind": "other",
                            "severity": "caution",
                            "description": body.description or "Field-reported hazard",
                            "poly_geojson": poly,
                        }
                    ],
                )
                if hazard_ids:
                    rasterize_hazard_to_hex_flags(mission_id, hazard_ids[0])
        except Exception as e:
            # Hazard marking is a best-effort side effect; don't fail the
            # finding insert if it errors. Log loudly so we notice in dev.
            logger.error("hazard hex-marking failed for finding %s: %s", finding_id, e)

    return FindingResponse(finding_id=finding_id, hex_id=hex_id)
