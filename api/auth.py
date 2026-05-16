"""Bearer-token FastAPI dependencies."""
from __future__ import annotations

import logging
import os

from fastapi import Header, HTTPException

from api.db.users import get_user_by_token

logger = logging.getLogger(__name__)

_ADMIN_TOKEN_WARNED = False


def _admin_token() -> str:
    global _ADMIN_TOKEN_WARNED
    token = os.environ.get("ADMIN_BEARER_TOKEN", "dev-admin-token")
    if token == "dev-admin-token" and not _ADMIN_TOKEN_WARNED:
        logger.warning(
            "ADMIN_BEARER_TOKEN env var not set — using insecure default 'dev-admin-token'"
        )
        _ADMIN_TOKEN_WARNED = True
    return token


async def current_user(x_bearer_token: str = Header(...)) -> dict:
    """Resolve bearer token to a user dict. Raises 401 on miss."""
    user = get_user_by_token(x_bearer_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return user


async def admin_user(x_bearer_token: str = Header(...)) -> None:
    """Verify the bearer token matches the admin token. Raises 401 on mismatch."""
    if x_bearer_token != _admin_token():
        raise HTTPException(status_code=401, detail="Admin access required")
