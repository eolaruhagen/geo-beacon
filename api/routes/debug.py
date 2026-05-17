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


class RestoreResponse(BaseModel):
    snapshot_path: str
    mission_id: int
    user_id: int
    callsign: Optional[str]
    bearer_token: str


@router.post("/restore", response_model=RestoreResponse, status_code=201)
async def debug_restore() -> RestoreResponse:
    """Restore the OLDEST snapshot file as the live DB, keep one searcher.

    Demo flow:
      1. Find the oldest snapshot in MISSION_SNAPSHOT_DIR.
      2. sqlite3.backup() it over the live DB.
      3. Apply any pending migrations (the snapshot may predate columns
         the running code requires — e.g. users.is_observer in 006).
      4. Pick the lowest-id searcher to keep; re-attribute every other
         user's pings/findings/dispatches/coverage/assignments to them.
      5. Delete the other users.
      6. Mark the kept user is_observer=1 and reset their status to
         standby. Cancel any in-flight dispatches and unassign segments
         so the agent gets a clean slate to dispatch from.

    No bearer-token auth — the snapshot rewrites the users table, so the
    caller's token would be invalidated mid-request anyway. Demo-only.
    """
    snaps = sorted(SNAPSHOT_DIR.glob("mission-*.db"), key=lambda p: p.stat().st_mtime)
    if not snaps:
        raise HTTPException(status_code=404, detail=f"No snapshots in {SNAPSHOT_DIR}")
    oldest = snaps[0]

    target_path = db_path()
    src = sqlite3.connect(str(oldest), timeout=30)
    try:
        dst = sqlite3.connect(target_path, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # Make sure the restored DB has every migration applied — including
    # any added since the snapshot was taken.
    from scripts.apply_migrations import apply, DEFAULT_MIGRATIONS_DIR
    apply(target_path, DEFAULT_MIGRATIONS_DIR)

    with session() as conn:
        conn.execute("BEGIN")
        try:
            kept = conn.execute(
                """
                SELECT id, callsign, bearer_token, current_mission_id
                FROM users WHERE role = 'searcher'
                ORDER BY id ASC LIMIT 1
                """
            ).fetchone()
            if kept is None:
                raise HTTPException(
                    status_code=500,
                    detail="Restored snapshot has no searcher users",
                )
            kept_id = kept["id"]
            mission_id = kept["current_mission_id"]
            if mission_id is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"Kept user {kept_id} has no current_mission_id",
                )

            # Re-attribute data from all other users to the kept user so the
            # restored map keeps its lived-in look without any orphan FKs.
            conn.execute("UPDATE pings        SET user_id = ?           WHERE user_id != ?",          (kept_id, kept_id))
            conn.execute("UPDATE findings     SET reporter_user_id = ?  WHERE reporter_user_id != ?", (kept_id, kept_id))
            conn.execute("UPDATE dispatches   SET user_id = ?           WHERE user_id != ?",          (kept_id, kept_id))
            conn.execute(
                "UPDATE hex_cells SET searched_by_user_id = ? "
                "WHERE searched_by_user_id IS NOT NULL AND searched_by_user_id != ?",
                (kept_id, kept_id),
            )
            conn.execute(
                "UPDATE segments SET assigned_user_id = ? "
                "WHERE assigned_user_id IS NOT NULL AND assigned_user_id != ?",
                (kept_id, kept_id),
            )

            # Drop everyone else. We allow this because we just re-attributed
            # every FK that pointed at them.
            conn.execute("DELETE FROM users WHERE id != ?", (kept_id,))

            # Clean demo start: cancel any in-flight dispatches and unassign
            # only the segments they touched (preserve swept/cleared).
            conn.execute(
                "UPDATE dispatches SET status = 'cancelled' "
                "WHERE status IN ('pending', 'acked', 'in_progress')"
            )
            conn.execute(
                "UPDATE segments SET assigned_user_id = NULL, status = 'unassigned' "
                "WHERE status IN ('assigned', 'in_progress')"
            )

            # Mark kept user observer + reset status.
            conn.execute(
                "UPDATE users SET is_observer = 1, status = 'standby' WHERE id = ?",
                (kept_id,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    logger.info(
        "restored snapshot %s; kept user_id=%s in mission %s",
        oldest.name, kept_id, mission_id,
    )
    return RestoreResponse(
        snapshot_path=str(oldest),
        mission_id=mission_id,
        user_id=kept_id,
        callsign=kept["callsign"],
        bearer_token=kept["bearer_token"],
    )


class DemoCredentials(BaseModel):
    mission_id: int
    user_id: int
    callsign: Optional[str]
    bearer_token: str


@router.get("/demo-credentials", response_model=DemoCredentials)
async def debug_demo_credentials() -> DemoCredentials:
    """Return the observer user's credentials so the phone can join as them
    without going through the regular /missions/join flow (which would create
    a fresh user row). No auth — purely a demo convenience endpoint.
    """
    with session() as conn:
        row = conn.execute(
            """
            SELECT id, callsign, bearer_token, current_mission_id
            FROM users WHERE is_observer = 1 LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="No observer user — run POST /debug/restore first",
            )
        if row["current_mission_id"] is None:
            raise HTTPException(
                status_code=409,
                detail="Observer user has no current_mission_id",
            )
        return DemoCredentials(
            mission_id=row["current_mission_id"],
            user_id=row["id"],
            callsign=row["callsign"],
            bearer_token=row["bearer_token"],
        )
