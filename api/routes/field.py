"""Field routes: ping ingestion, searcher state, and findings."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from api.auth import current_user
from api.schemas import FindingRequest, FindingResponse, MeResponse, PingRequest, PingResponse
import api.db.missions as db_missions
import api.db.pings as db_pings

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
    return MeResponse(
        user=user,
        mission_id=mission_id,
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

    try:
        from api.db.hex_cells import hex_cell_id_at, hex_cells_for_mission, set_flag_clue_for_hex

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

        if hex_id is not None:
            set_flag_clue_for_hex(hex_id)
    except Exception as e:
        logger.warning("hex resolution failed for finding: %s", e)

    try:
        from api.db import session
        import json as _json
        ts = int(time.time())
        with session() as db:
            cur = db.execute(
                """
                INSERT INTO findings
                    (mission_id, user_id, hex_id, lat, lon, kind, description, confidence, found_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mission_id, user["id"], hex_id, lat, lon,
                 body.kind, body.description, body.confidence, ts),
            )
            finding_id = cur.lastrowid
    except Exception as e:
        logger.error("insert finding failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to insert finding")

    return FindingResponse(finding_id=finding_id, hex_id=hex_id)
