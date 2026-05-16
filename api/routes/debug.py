"""Debug-only routes used to drive UI development before the agent layer exists.

These endpoints exist purely so the searcher app can be exercised end-to-end
without an agent in the loop. They mimic the side effects the real agent
skills (`dispatch_searcher`, etc.) will produce, so the UI code written against
them stays valid once the agent comes online.

**Strip or gate before any non-demo deploy.**
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from api.auth import current_user
from api.db import db_path, session
from api.schemas import ActiveDispatch, SweepType
import api.db.dispatches as db_dispatches
import api.db.missions as db_missions
import api.db.users as db_users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


class DebugDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_id: int
    sweep_type: Optional[SweepType] = "efficient"
    instruction: str = Field(default="Debug dispatch")
    reasoning: str = Field(default="Manual dispatch via /debug/dispatch")
    # Default: dispatch the caller to themselves. Pass a different value to
    # dispatch a teammate in the same mission.
    target_user_id: Optional[int] = None


@router.post("/dispatch", response_model=ActiveDispatch, status_code=201)
async def debug_dispatch(
    body: DebugDispatchRequest,
    caller: dict = Depends(current_user),
) -> ActiveDispatch:
    """Insert a pending dispatch + flip the segment and target-user state.

    Mirrors the agent's eventual `dispatch_searcher` skill closely enough that
    the searcher app can develop against this and not need to change calls
    when the real agent lands.
    """
    mission_id = db_missions.active_mission_id_for_user(caller["id"])
    if mission_id is None:
        raise HTTPException(status_code=409, detail="Caller has no active mission")

    target_user_id = body.target_user_id if body.target_user_id is not None else caller["id"]

    # Validate target is in the same mission. Without this you could
    # accidentally dispatch a user from another mission (or a nonexistent id).
    target = db_users.get_user(target_user_id)
    if target is None or target.get("current_mission_id") != mission_id:
        raise HTTPException(
            status_code=400,
            detail=f"target_user_id {target_user_id} is not a member of mission {mission_id}",
        )

    now = int(time.time())
    with session() as conn:
        # Confirm segment exists in this mission and pull its centroid for the
        # entry_point default. Centroid is a reasonable stand-in until the
        # agent picks a smarter entry.
        seg = conn.execute(
            """
            SELECT id, X(Centroid(geom)) AS clon, Y(Centroid(geom)) AS clat
            FROM segments WHERE id = ? AND mission_id = ?
            """,
            (body.segment_id, mission_id),
        ).fetchone()
        if seg is None:
            raise HTTPException(
                status_code=404,
                detail=f"Segment {body.segment_id} not found in mission {mission_id}",
            )

        cur = conn.execute(
            """
            INSERT INTO dispatches (
                mission_id, user_id, segment_id, sweep_type,
                entry_lat, entry_lon, instruction, reasoning,
                status, issued_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                mission_id,
                target_user_id,
                body.segment_id,
                body.sweep_type,
                float(seg["clat"]),
                float(seg["clon"]),
                body.instruction,
                body.reasoning,
                now,
            ),
        )
        dispatch_id = cur.lastrowid

        # Same side effects the agent will produce when it dispatches: flip
        # the searcher to 'dispatched' and tag the segment as assigned to them.
        conn.execute(
            "UPDATE users SET status = 'dispatched' WHERE id = ?",
            (target_user_id,),
        )
        conn.execute(
            """
            UPDATE segments
            SET status = 'assigned', assigned_user_id = ?, sweep_type = ?
            WHERE id = ?
            """,
            (target_user_id, body.sweep_type, body.segment_id),
        )

    # Reload through the canonical helper so the response shape matches
    # /field/me.active_dispatch byte-for-byte.
    row = db_dispatches.get_dispatch(dispatch_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Dispatch insert succeeded but row not found")
    return ActiveDispatch.model_validate(row)


SNAPSHOT_DIR = Path(os.environ.get("MISSION_SNAPSHOT_DIR", "/tmp/geo-beacon-snapshots"))


class SnapshotResponse(BaseModel):
    path: str
    bytes: int
    ts: int


@router.post("/snapshot", response_model=SnapshotResponse, status_code=201)
async def debug_snapshot(
    caller: dict = Depends(current_user),
) -> SnapshotResponse:
    """Snapshot the live SQLite DB to /tmp for demo rollbacks.

    Uses sqlite3's online backup API, which is safe under WAL while readers
    and writers are active. The output is a single self-contained .db file
    (no separate -wal/-shm) named with the unix timestamp, so re-running
    yields a fresh file rather than overwriting the last snapshot.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    dest_path = SNAPSHOT_DIR / f"mission-{ts}.db"

    src_path = db_path()
    src_conn = sqlite3.connect(src_path, timeout=30)
    try:
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()

    size_bytes = dest_path.stat().st_size
    logger.info(
        "snapshot written by user_id=%s: %s (%d bytes)",
        caller["id"], dest_path, size_bytes,
    )
    return SnapshotResponse(path=str(dest_path), bytes=size_bytes, ts=ts)
