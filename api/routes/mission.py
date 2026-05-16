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
    user: dict = Depends(current_user),
) -> Response:
    if mission_id is None:
        mission_id = db_missions.active_mission_id_for_user(user["id"])
    if mission_id is None:
        raise HTTPException(status_code=404, detail="No active mission")

    mission = db_missions.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    # AUTH-1: cross-mission read protection. Without this, any authenticated
    # token could enumerate mission_ids and scrape segments / searchers /
    # findings / hazards from missions the user has no relationship with.
    # The creator is implicitly an admin; everyone else must be currently
    # joined (current_mission_id match) to read.
    is_creator = user["id"] == mission["created_by_user_id"]
    is_member  = user.get("current_mission_id") == mission_id
    if not (is_creator or is_member):
        raise HTTPException(status_code=403, detail="Not a member of this mission")

    fc = db_geojson.mission_state_feature_collection(mission_id)
    return Response(
        content=json.dumps(fc),
        media_type="application/geo+json",
    )
