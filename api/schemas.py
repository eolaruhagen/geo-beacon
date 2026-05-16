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


class MeResponse(BaseModel):
    user: UserPublic
    mission_id: Optional[int] = None
    # active_dispatch / segment_geojson will become populated optionals when the
    # dispatch endpoints land (SPEC-2). For now they're null but the field types
    # are Optional[Any] so the wire shape stays stable across that change.
    active_dispatch: Optional[Any] = None
    segment_geojson: Optional[Any] = None
    nearby_hazards: List = Field(default_factory=list)
    recent_broadcasts: List = Field(default_factory=list)
