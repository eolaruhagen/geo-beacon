"""Pydantic v2 request/response models for all API endpoints."""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

VALID_HAZARD_KINDS = {"cliff", "water", "weather", "no_comms_zone", "wildlife", "other"}
VALID_HAZARD_SEVERITIES = {"info", "caution", "critical"}
VALID_ROLES = {"searcher", "observer"}


class HazardInput(BaseModel):
    kind: str
    severity: str
    description: Optional[str] = None
    poly_geojson: dict[str, Any]

    @model_validator(mode="after")
    def validate_kind_severity(self) -> "HazardInput":
        if self.kind not in VALID_HAZARD_KINDS:
            raise ValueError(f"kind must be one of {VALID_HAZARD_KINDS}")
        if self.severity not in VALID_HAZARD_SEVERITIES:
            raise ValueError(f"severity must be one of {VALID_HAZARD_SEVERITIES}")
        if self.poly_geojson.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError("poly_geojson must be a GeoJSON Polygon or MultiPolygon")
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
        geom = self.area_geojson
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError("area_geojson must be a GeoJSON Polygon or MultiPolygon")
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
    role: Optional[str] = None

    @model_validator(mode="after")
    def validate_role(self) -> "JoinMissionRequest":
        if self.role is not None and self.role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        return self


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


class PingResponse(BaseModel):
    ping_id: int


class FindingRequest(BaseModel):
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    hex_id: Optional[int] = None
    kind: str = Field(min_length=1)
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


class MeResponse(BaseModel):
    user: Any
    mission_id: Optional[int] = None
    active_dispatch: None = None
    segment_geojson: None = None
    nearby_hazards: List = Field(default_factory=list)
    recent_broadcasts: List = Field(default_factory=list)
