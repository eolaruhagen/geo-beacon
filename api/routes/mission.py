"""Mission routes: GeoJSON state endpoint."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response

from api.auth import current_user
import api.db.missions as db_missions
import api.db.geojson as db_geojson
import json

router = APIRouter(prefix="/mission", tags=["mission"])


@router.get("/state.geojson")
async def mission_state(
    mission_id: Optional[int] = None,
    _user: dict = Depends(current_user),
) -> Response:
    if mission_id is None:
        mission_id = db_missions.active_mission_id_for_user(_user["id"])
    if mission_id is None:
        raise HTTPException(status_code=404, detail="No active mission")

    if db_missions.get_mission(mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    fc = db_geojson.mission_state_feature_collection(mission_id)
    return Response(
        content=json.dumps(fc),
        media_type="application/geo+json",
    )
