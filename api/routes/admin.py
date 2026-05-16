"""Admin routes: mission creation and user provisioning."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from api.auth import admin_user
from api.schemas import (
    CreateMissionRequest,
    CreateMissionResponse,
    CreateUserRequest,
    CreateUserResponse,
)
import api.db.missions as db_missions
import api.db.users as db_users
import api.db.gate as db_gate
import api.db.segments as db_segments

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/mission", response_model=CreateMissionResponse, dependencies=[Depends(admin_user)])
async def create_mission(body: CreateMissionRequest) -> CreateMissionResponse:
    mission_id = db_missions.create_mission(
        name=body.name,
        subject_description=body.subject_description,
        pls_lat=body.pls_lat,
        pls_lon=body.pls_lon,
        pls_ts=body.pls_ts,
        area_geojson=body.area_geojson,
    )

    terrain_result = {"terrain_cells_inserted": 0, "osm_features_inserted": 0}
    n_segments = 0

    try:
        from scripts.fetch_terrain import fetch_terrain
        terrain_result = fetch_terrain(mission_id)
    except Exception as e:
        logger.warning("fetch_terrain failed (mission_id=%s): %s — continuing with empty terrain", mission_id, e)

    try:
        from scripts.seed_segments import seed_segments
        n_segments = seed_segments(mission_id)
    except Exception as e:
        logger.warning("seed_segments failed (mission_id=%s): %s — continuing with 0 segments", mission_id, e)

    hazard_counts: dict[str, int] = {"total": 0}
    try:
        from scripts.seed_hazards import seed_hazards
        hazard_counts = seed_hazards(mission_id)
    except Exception as e:
        logger.warning("seed_hazards failed (mission_id=%s): %s — continuing with 0 hazards", mission_id, e)

    if hazard_counts.get("total", 0) > 0 and n_segments > 0:
        try:
            penalty = db_segments.apply_hazard_penalty(mission_id)
            logger.info("apply_hazard_penalty mission=%s: %s", mission_id, penalty)
        except Exception as e:
            logger.warning("apply_hazard_penalty failed (mission_id=%s): %s", mission_id, e)

    db_missions.set_status(mission_id, "active")
    db_gate.enqueue_trigger(mission_id, "mission_start")

    return CreateMissionResponse(
        mission_id=mission_id,
        n_segments=n_segments,
        n_terrain_cells=terrain_result.get("terrain_cells_inserted", 0),
        n_hazards=hazard_counts.get("total", 0),
    )


@router.post("/users", response_model=CreateUserResponse, dependencies=[Depends(admin_user)])
async def create_user(body: CreateUserRequest) -> CreateUserResponse:
    user = db_users.create_user(
        display_name=body.display_name,
        callsign=body.callsign,
        role=body.role,
    )
    return CreateUserResponse(
        user_id=user["id"],
        bearer_token=user["bearer_token"],
        callsign=user["callsign"],
    )
