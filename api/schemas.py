"""Pydantic v2 request/response models for all API endpoints."""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


VALID_ROLES = {"searcher", "team_leader", "observer"}


class CreateMissionRequest(BaseModel):
    name: str = Field(min_length=1)
    subject_description: str = Field(min_length=1)
    pls_lat: float = Field(ge=-90, le=90)
    pls_lon: float = Field(ge=-180, le=180)
    pls_ts: int
    area_geojson: dict[str, Any]

    @model_validator(mode="after")
    def validate_area_geojson(self) -> "CreateMissionRequest":
        geom = self.area_geojson
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError("area_geojson must be a GeoJSON Polygon or MultiPolygon")
        return self


class CreateMissionResponse(BaseModel):
    mission_id: int
    n_segments: int
    n_terrain_cells: int
    n_hazards: int = 0


class CreateUserRequest(BaseModel):
    display_name: str = Field(min_length=1)
    callsign: Optional[str] = None
    role: Literal["searcher", "team_leader", "observer"]


class CreateUserResponse(BaseModel):
    user_id: int
    bearer_token: str
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


class MeResponse(BaseModel):
    user: Any
    active_dispatch: None = None
    segment_geojson: None = None
    nearby_hazards: List = Field(default_factory=list)
    recent_broadcasts: List = Field(default_factory=list)
