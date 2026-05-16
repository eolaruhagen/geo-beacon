"""Field routes: ping ingestion and searcher state."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from api.auth import current_user
from api.schemas import MeResponse, PingRequest, PingResponse
import api.db.missions as db_missions
import api.db.pings as db_pings

router = APIRouter(prefix="/field", tags=["field"])


@router.post("/ping", response_model=PingResponse)
async def field_ping(
    body: PingRequest,
    user: dict = Depends(current_user),
) -> PingResponse:
    mission_id = db_missions.active_mission_id()
    if mission_id is None:
        raise HTTPException(status_code=409, detail="No active mission")

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
    return MeResponse(user=user)
