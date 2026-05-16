"""Bearer-token FastAPI dependencies."""
from __future__ import annotations

import logging

from fastapi import Depends, Header, HTTPException

from api.db.users import get_user_by_token
import api.db.missions as db_missions

logger = logging.getLogger(__name__)


async def current_user(x_bearer_token: str = Header(...)) -> dict:
    """Resolve bearer token to a user dict. Raises 401 on miss."""
    user = get_user_by_token(x_bearer_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return user


async def admin_for_mission(
    mission_id: int,
    user: dict = Depends(current_user),
) -> dict:
    """403 unless the current user is the mission creator."""
    mission = db_missions.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    if user["id"] != mission["created_by_user_id"]:
        raise HTTPException(status_code=403, detail="Mission admin access required")
    return user
