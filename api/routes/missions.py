"""Mission creation and join routes (public — no auth required)."""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException

from api.schemas import (
    CreateMissionRequest,
    CreateMissionResponse,
    JoinMissionRequest,
    JoinMissionResponse,
)
import api.db.missions as db_missions
import api.db.users as db_users
import api.db.segments as db_segments
import api.db.hazards as db_hazards

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/missions", tags=["missions"])


@router.post("", response_model=CreateMissionResponse, status_code=201)
async def create_mission(body: CreateMissionRequest) -> CreateMissionResponse:
    user = db_users.create_user(
        display_name=body.display_name,
        callsign=body.callsign,
        role="searcher",
    )

    join_code = secrets.token_hex(3)  # 6 hex chars, easy to type

    mission_id = db_missions.create_mission(
        name=body.name,
        subject_description=body.subject_description,
        pls_lat=body.pls_lat,
        pls_lon=body.pls_lon,
        pls_ts=body.pls_ts,
        area_geojson=body.area_geojson,
        created_by_user_id=user["id"],
        join_code=join_code,
    )

    hex_data: list[dict] = []
    try:
        from scripts.fetch_terrain import fetch_terrain
        terrain_result = fetch_terrain(mission_id)
        hex_data = terrain_result.get("hex_data", [])
    except Exception as e:
        logger.warning("fetch_terrain failed (mission_id=%s): %s — continuing", mission_id, e)

    segment_ids: list[int] = []
    n_segments = 0
    try:
        from scripts.seed_segments import seed_segments
        segment_ids = seed_segments(mission_id, hex_data)
        n_segments = len(segment_ids)
    except Exception as e:
        logger.warning("seed_segments failed (mission_id=%s): %s — continuing", mission_id, e)

    n_hex = 0
    try:
        from scripts.seed_hex_cells import seed_hex_cells
        n_hex = seed_hex_cells(mission_id, hex_data, segment_ids)
    except Exception as e:
        logger.warning("seed_hex_cells failed (mission_id=%s): %s — continuing", mission_id, e)

    hazard_counts: dict[str, int] = {"total_hazards": 0}
    try:
        from scripts.seed_hazards import seed_hazards
        hazard_counts = seed_hazards(mission_id)
    except Exception as e:
        logger.warning("seed_hazards failed (mission_id=%s): %s — continuing", mission_id, e)

    n_hazards = hazard_counts.get("total_hazards", 0)

    if body.hazards:
        try:
            hazard_rows = [h.model_dump() for h in body.hazards]
            inserted_ids = db_hazards.bulk_insert_hazards(mission_id, hazard_rows)
            n_hazards += len(inserted_ids)
            try:
                from api.db.hex_cells import rasterize_hazard_to_hex_flags
                for h_id in inserted_ids:
                    rasterize_hazard_to_hex_flags(mission_id, h_id)
            except Exception as e:
                logger.warning("rasterize custom hazards failed: %s", e)
        except Exception as e:
            logger.warning("bulk_insert_hazards failed: %s", e)

    if n_segments > 0:
        try:
            db_segments.apply_hazard_penalty(mission_id)
        except Exception as e:
            logger.warning("apply_hazard_penalty failed (mission_id=%s): %s", mission_id, e)

    db_missions.set_status(mission_id, "active")

    return CreateMissionResponse(
        mission_id=mission_id,
        join_code=join_code,
        bearer_token=user["bearer_token"],
        user_id=user["id"],
        n_segments=n_segments,
        n_hex_cells=n_hex,
        n_hazards=n_hazards,
    )


@router.post("/join", response_model=JoinMissionResponse, status_code=201)
async def join_mission(body: JoinMissionRequest) -> JoinMissionResponse:
    mission = db_missions.get_mission_by_join_code(body.join_code)
    if mission is None:
        raise HTTPException(status_code=404, detail="Join code not found")

    user = db_users.create_user(
        display_name=body.display_name,
        callsign=body.callsign,
        role=body.role or "searcher",
    )

    return JoinMissionResponse(
        mission_id=mission["id"],
        bearer_token=user["bearer_token"],
        user_id=user["id"],
        callsign=user["callsign"],
    )
