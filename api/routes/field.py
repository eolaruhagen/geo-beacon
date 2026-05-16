"""Field routes: ping ingestion, searcher state, and findings."""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from api.auth import current_user
from api.db import session
from api.db.hazards import bulk_insert_hazards
from api.db.hex_cells import (
    hex_cell_id_at,
    hex_cells_for_mission,
    rasterize_hazard_to_hex_flags,
    set_flag_clue_for_hex,
)
from api.schemas import (
    ActiveDispatch,
    DispatchActionResponse,
    DispatchCompleteRequest,
    FindingRequest,
    FindingResponse,
    MeResponse,
    PingRequest,
    PingResponse,
    UserPublic,
)
import api.db.dispatches as db_dispatches
import api.db.missions as db_missions
import api.db.pings as db_pings
import api.db.users as db_users

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

    return MeResponse(
        user=UserPublic.model_validate(user),
        mission_id=mission_id,
        active_dispatch=active_model,
        segment_geojson=seg_feature,
    )


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
