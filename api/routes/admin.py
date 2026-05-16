"""Admin routes: agent invocation and mission finish (mission creator only)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from api.auth import admin_for_mission
import api.db.missions as db_missions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/agent/invoke")
async def agent_invoke() -> dict:
    raise HTTPException(status_code=501, detail="agent loop not implemented")


@router.post("/mission/{mission_id}/finish")
async def finish_mission(
    mission_id: int,
    _user: dict = Depends(admin_for_mission),
) -> dict:
    db_missions.set_status(mission_id, "ended")
    return {"ok": True}
