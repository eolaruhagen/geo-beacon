"""Pydantic v2 request/response models for all API endpoints."""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Literal aliases mirror the DB CHECK constraints in migrations/. Keep these
# in sync if a migration changes the allowed values.
FindingKind = Literal[
    "clue",
    "subject_found",
    "subject_sighting",
    "hazard",
    "footprint",
    "discarded_item",
    "note",
    "other",
]  # migrations/002_spatial.sql:74 (findings.kind CHECK)

HazardKind = Literal[
    "cliff", "water", "weather", "no_comms_zone", "wildlife", "other"
]  # migrations/002_spatial.sql:97 (hazards.kind CHECK)

HazardSeverity = Literal[
    "info", "caution", "critical"
]  # migrations/002_spatial.sql:98 (hazards.severity CHECK)

UserRole = Literal["searcher", "observer"]  # migrations/001_init.sql:19 (users.role CHECK)

DispatchStatus = Literal[
    "pending", "acked", "in_progress", "completed", "cancelled", "superseded"
]  # migrations/001_init.sql:59 (dispatches.status CHECK)

SweepType = Literal["hasty", "efficient", "thorough"]  # migrations/001_init.sql:54

BroadcastKind = Literal[
    "info", "warning", "recall", "finding_alert", "route_correction"
]  # migrations/001_init.sql:73 (broadcasts.kind CHECK)


class HazardInput(BaseModel):
    # H-2: hazards.geom is registered as POLYGON (singular) in
    # migrations/002_spatial.sql:103 — SpatiaLite rejects MultiPolygon at INSERT.
    kind: HazardKind
    severity: HazardSeverity
    description: str = Field(min_length=1)  # hazards.description is NOT NULL
    poly_geojson: dict[str, Any]

    @model_validator(mode="after")
    def validate_poly_geojson(self) -> "HazardInput":
        if self.poly_geojson.get("type") != "Polygon":
            raise ValueError("poly_geojson must be a GeoJSON Polygon (MultiPolygon not supported)")
        return self


class CreateMissionRequest(BaseModel):
    name: str = Field(min_length=1)
    subject_description: str = Field(min_length=1)
    pls_lat: float = Field(ge=-90, le=90)
    pls_lon: float = Field(ge=-180, le=180)
    pls_ts: int
    area_geojson: dict[str, Any]
    display_name: str = Field(min_length=1)
    callsign: Optional[str] = None
    hazards: Optional[List[HazardInput]] = None

    @model_validator(mode="after")
    def validate_area_geojson(self) -> "CreateMissionRequest":
        # MI-1: missions.area_geom is POLYGON (migrations/002_spatial.sql:16).
        # SpatiaLite rejects MultiPolygon at INSERT, so reject it up front.
        geom_type = self.area_geojson.get("type")
        if geom_type != "Polygon":
            raise ValueError("area_geojson must be a GeoJSON Polygon (MultiPolygon not supported)")
        return self


class CreateMissionResponse(BaseModel):
    mission_id: int
    join_code: str
    bearer_token: str
    user_id: int
    n_segments: int
    n_hex_cells: int
    n_hazards: int


class JoinMissionRequest(BaseModel):
    join_code: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    callsign: Optional[str] = None
    role: Optional[UserRole] = None  # users.role CHECK (migrations/001_init.sql:19)


class JoinMissionResponse(BaseModel):
    mission_id: int
    bearer_token: str
    user_id: int
    callsign: Optional[str]


class PingRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    ts: Optional[int] = None
    accuracy_m: Optional[float] = None
    speed_mps: Optional[float] = None
    battery_pct: Optional[int] = Field(default=None, ge=0, le=100)
    # NOTE: pings.source is NOT NULL with CHECK ('phone','replay','manual') but
    # is set server-side (default 'phone' for this endpoint), so not on the wire.


class PingResponse(BaseModel):
    ping_id: int


class FindingRequest(BaseModel):
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    hex_id: Optional[int] = None
    # F-3: findings.kind CHECK in migrations/002_spatial.sql:74.
    kind: FindingKind
    # F-2: findings.description is nullable per migrations/002_spatial.sql.
    description: Optional[str] = None
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def require_lat_lon_or_hex_id(self) -> "FindingRequest":
        has_latlon = self.lat is not None and self.lon is not None
        has_hex = self.hex_id is not None
        if not has_latlon and not has_hex:
            raise ValueError("Either (lat, lon) or hex_id must be provided")
        return self


class FindingResponse(BaseModel):
    finding_id: int
    hex_id: Optional[int]


class UserPublic(BaseModel):
    # Safe-to-expose projection of users — drops bearer_token and phone (PYD-1).
    # Used in MeResponse and anywhere else a user is returned to a caller.
    model_config = ConfigDict(extra="ignore")

    id: int
    display_name: str
    callsign: Optional[str]
    role: UserRole
    status: str
    current_mission_id: Optional[int]


class ActiveDispatch(BaseModel):
    """Single in-flight dispatch for a searcher (status in pending/acked/in_progress)."""
    model_config = ConfigDict(extra="ignore")

    id: int
    mission_id: int
    user_id: int
    segment_id: Optional[int]
    sweep_type: Optional[SweepType]
    entry_lat: Optional[float]
    entry_lon: Optional[float]
    instruction: str
    reasoning: str
    status: DispatchStatus
    issued_ts: int
    acked_ts: Optional[int]
    started_ts: Optional[int]
    completed_ts: Optional[int]


class DispatchCompleteRequest(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=2000)


class DispatchActionResponse(BaseModel):
    dispatch_id: int
    status: DispatchStatus
    user_status: str


class Broadcast(BaseModel):
    """Single broadcast row, already filtered through the visibility policy
    in api/db/broadcasts.py (scope is 'all' or 'user:{caller_id}')."""
    model_config = ConfigDict(extra="ignore")

    id: int
    scope: str
    kind: BroadcastKind
    message: str
    ts: int


class AnnouncementsResponse(BaseModel):
    """Return shape for GET /field/announcements?since=ts.

    `cursor_ts` is the latest broadcast `ts` in this batch (or echo of
    `since` if empty). The app stores this and re-polls with
    `?since=cursor_ts` for incremental delivery.
    """
    broadcasts: List[Broadcast]
    cursor_ts: int


class RouteWaypoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class RouteResponse(BaseModel):
    """Snap-to-trail route from the searcher's last known position to the
    target segment's entry point. Spec §13 query_route: snap only, no
    along-trail path. App renders a polyline through `waypoints` in order.

    `snapped`: false when the mission has no trail features (or the closest
    is degenerate) — in that case `waypoints` is just [start, target] and
    the app should render a bee-line.
    """
    waypoints: List[RouteWaypoint]
    snapped: bool


class MeResponse(BaseModel):
    user: UserPublic
    mission_id: Optional[int] = None
    active_dispatch: Optional[ActiveDispatch] = None
    # GeoJSON Feature for the active dispatch's segment (with properties:
    # name, poa, pod, sweep_type, terrain stats). None when no active dispatch
    # or the dispatch is a recall (segment_id NULL).
    segment_geojson: Optional[dict[str, Any]] = None
    nearby_hazards: List = Field(default_factory=list)
    # Already scope-filtered via api/db/broadcasts.visible_broadcasts_for_user.
    # Capped to the most recent few so the 5s poll stays cheap; full history
    # lives behind GET /field/announcements?since=ts.
    recent_broadcasts: List[Broadcast] = Field(default_factory=list)
