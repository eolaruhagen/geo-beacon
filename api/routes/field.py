"""Field routes: ping ingestion, searcher state, and findings."""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from api.auth import current_user
from api.db import session
from api.db.hazards import bulk_insert_hazards
from api.db.hex_cells import (
    hex_cell_id_at,
    hex_cells_for_mission,
    rasterize_hazard_to_hex_flags,
    set_flag_clue_for_hex,
)
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

    # Resolve hex_id from either provided hex_id or lat/lon. Narrow except so a
    # genuine bug in the resolver doesn't get swallowed.
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

    # hex_id is NOT NULL in the schema. If we couldn't resolve a containing
    # hex (point outside the mission grid), surface a clean 422 rather than
    # 500 on the NULL insert.
    if hex_id is None:
        raise HTTPException(
            status_code=422,
            detail="Point is outside any hex cell for the active mission",
        )
    if lat is None or lon is None:
        raise HTTPException(
            status_code=422,
            detail="Could not resolve lat/lon for the given hex_id",
        )

    set_flag_clue_for_hex(hex_id)

    ts = int(time.time())
    try:
        with session() as db:
            cur = db.execute(
                """
                INSERT INTO findings
                    (mission_id, reporter_user_id, hex_id, ts, lat, lon, kind, description, confidence, geom)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, SetSRID(MakePoint(?, ?), 4326))
                """,
                (
                    mission_id,
                    user["id"],
                    hex_id,
                    ts,
                    lat,
                    lon,
                    body.kind,
                    body.description,
                    body.confidence,
                    lon,
                    lat,
                ),
            )
            finding_id = cur.lastrowid
    except Exception as e:
        logger.error("insert finding failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to insert finding")

    # Hex-marking branch: when a searcher taps "hazard", drop a hazard polygon
    # matching the containing hex and rasterize its flag_danger onto the grid.
    # findings.kind='hazard' (generic) maps to hazards.kind='other' since the
    # two enums don't align 1:1.
    if body.kind == "hazard":
        try:
            with session() as db:
                row = db.execute(
                    "SELECT AsGeoJSON(geom) AS poly_geojson FROM hex_cells WHERE id = ?",
                    (hex_id,),
                ).fetchone()
            if row and row["poly_geojson"]:
                poly = json.loads(row["poly_geojson"])
                hazard_ids = bulk_insert_hazards(
                    mission_id,
                    [
                        {
                            "kind": "other",
                            "severity": "caution",
                            "description": body.description or "Field-reported hazard",
                            "poly_geojson": poly,
                        }
                    ],
                )
                if hazard_ids:
                    rasterize_hazard_to_hex_flags(mission_id, hazard_ids[0])
        except Exception as e:
            # Hazard marking is a best-effort side effect; don't fail the
            # finding insert if it errors. Log loudly so we notice in dev.
            logger.error("hazard hex-marking failed for finding %s: %s", finding_id, e)

    return FindingResponse(finding_id=finding_id, hex_id=hex_id)
