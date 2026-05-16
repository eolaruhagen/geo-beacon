# Inter-layer contracts — mission onboarding slice

Pinned function signatures so the DB / pipeline / API layers can be built in
parallel without renegotiating. Anything not listed here is implementation
detail and may change freely inside the owning layer.

All timestamps are unix-epoch INTEGER seconds. All lat/lon are WGS84 floats.
SQLite connection is obtained via `from api.db import session` (yields a
SpatiaLite-loaded connection — context-managed, see `api/db/__init__.py`).

---

## Layer 1 — DB helpers (`api/db/`)

Pure DB ops. No FastAPI imports, no HTTP, no geospatial math beyond calling
SpatiaLite SQL functions.

### `api/db/missions.py`

```python
def create_mission(
    name: str,
    subject_description: str,
    pls_lat: float,
    pls_lon: float,
    pls_ts: int,
    area_geojson: dict,           # GeoJSON Polygon dict
) -> int:
    """Insert mission row with area_geom from GeoJSON. status='planning'.
    Returns new mission_id. Sets started_ts = now."""

def get_mission(mission_id: int) -> dict | None:
    """All columns plus area_geom as GeoJSON dict (key 'area_geojson')."""

def set_status(mission_id: int, status: str) -> None:
    """Update mission.status. Sets ended_ts when transitioning to ended."""

def active_mission_id() -> int | None:
    """Returns id of the single mission with status='active', else None.
    Spec §2 assumes single concurrent mission."""
```

### `api/db/users.py`

```python
def create_user(
    display_name: str,
    callsign: str | None,
    role: str,                    # 'searcher' | 'team_leader' | 'observer'
) -> dict:
    """Inserts user with status='standby', random hex bearer_token (32 bytes).
    Returns {id, display_name, callsign, role, status, bearer_token, created_ts}."""

def get_user_by_token(token: str) -> dict | None:
    """Bearer-token lookup. Returns full user row or None."""

def get_user(user_id: int) -> dict | None: ...
```

### `api/db/pings.py`

```python
def insert_ping(
    user_id: int,
    mission_id: int,
    lat: float,
    lon: float,
    ts: int,
    accuracy_m: float | None = None,
    speed_mps: float | None = None,
    battery_pct: int | None = None,
    source: str = "phone",         # 'phone' | 'replay' | 'manual'
) -> int:
    """Inserts row. geom = MakePoint(lon, lat, 4326). Returns ping_id."""
```

### `api/db/segments.py`

```python
SegmentRow = dict   # keys: name, poly_geojson (Polygon dict), area_m2, poa,
                    #       avg_slope_deg, dominant_cover, trail_length_m

def bulk_insert_segments(mission_id: int, rows: list[SegmentRow]) -> int:
    """Insert all rows in one transaction. status='unassigned'. Returns count."""

def segments_for_mission(mission_id: int) -> list[dict]:
    """All columns plus geom as GeoJSON dict (key 'geom_geojson')."""
```

### `api/db/terrain.py`

```python
TerrainCell = dict   # keys: poly_geojson, center_elev_m, avg_slope_deg, dominant_cover
OSMFeature  = dict   # keys: kind, name (optional), geom_geojson (Polygon or LineString)

def bulk_insert_terrain_cells(mission_id: int, cells: list[TerrainCell]) -> int: ...
def bulk_insert_osm_features(mission_id: int, features: list[OSMFeature]) -> int: ...
def terrain_cells_for_mission(mission_id: int) -> list[dict]: ...
def osm_features_for_mission(mission_id: int) -> list[dict]: ...
```

### `api/db/geojson.py`

```python
def mission_state_feature_collection(mission_id: int) -> dict:
    """Returns full GeoJSON FeatureCollection per spec §11:
      - segments: Polygon Features with properties {id, name, poa, pod, pos, status, sweep_type}
      - searchers: latest-ping Point Features with properties {user_id, callsign, status}
      - tracks: last-30-min Track LineString Features per searcher
      - findings: Point Features with properties {kind, description, confidence, ts}
      - hazards: Polygon Features with properties {kind, severity, description}
    Result is JSON-serializable."""
```

### `api/db/gate.py` (queue insert helper — minimal stub for now)

```python
def enqueue_trigger(mission_id: int, trigger: str, context: dict | None = None) -> int:
    """Inserts agent_invocation_queue row. Returns id. Worker not implemented yet."""
```

---

## Layer 2 — Pipelines + math

### `scripts/fetch_terrain.py`

CLI: `python scripts/fetch_terrain.py --mission-id N`

Programmatic entry:
```python
def fetch_terrain(mission_id: int) -> dict:
    """Reads mission.area_geom from DB. Computes bbox. Fetches:
      1) USGS NED 1/3 arcsec DEM (or fallback Open-Elevation if NED unreachable
         — fetch_terrain MUST not block on slow government endpoints during demo).
      2) ESA WorldCover 2021 classification (10m).
      3) OSM trails/roads/water/buildings via Overpass API.

    Resamples DEM + WorldCover to a ~100m grid covering the bbox.
    Calls api.db.terrain.bulk_insert_terrain_cells with rows shaped as TerrainCell.
    Calls api.db.terrain.bulk_insert_osm_features.

    Returns {terrain_cells_inserted: N, osm_features_inserted: M}.
    Idempotent: deletes existing rows for mission_id before inserting."""
```

Implementation may use `rasterio`, `numpy`, `requests`, `shapely` (all in requirements.txt).
For the hackathon, providing a `--mock` flag that generates a synthetic grid (uniform slope, mixed cover, fake trail running through bbox) is acceptable and recommended as a fallback path so the demo never depends on network reachability of public APIs.

### `scripts/seed_segments.py`

CLI: `python scripts/seed_segments.py --mission-id N`

```python
def seed_segments(mission_id: int) -> int:
    """Reads mission.area_geom + terrain_cells + osm_features for mission_id.
    Subdivides area into ~100m × 100m square grid clipped to area_geom.
    For each cell that intersects the mission area:
      - area_m2 from spatial calc
      - avg_slope_deg, dominant_cover by spatial-join to terrain_cells
      - trail_length_m by ST_Length(ST_Intersection(seg, osm trail union))
      - raw_w from spec §7 formula (dist_term · trail_term · downhill_term · cover_term)
    Normalize raw_w → poa. Bulk-insert via api.db.segments.bulk_insert_segments.

    Returns number of segments inserted."""
```

### `agent/poa.py`

Pure functions, no DB:

```python
def initial_poa_weights(
    cell_centers: list[tuple[float, float]],   # (lat, lon) per cell
    cell_elev_m: list[float],
    cell_cover: list[str],
    cell_has_trail: list[bool],
    pls_lat: float,
    pls_lon: float,
    pls_elev_m: float,
    sigma_m: float = 750.0,
) -> list[float]:
    """Computes the raw_w array per spec §7. Returns un-normalized weights;
    caller normalizes."""
```

---

## Layer 3 — FastAPI routes + plumbing

### `api/main.py`

```python
# - FastAPI app
# - On startup: run scripts.apply_migrations.apply()
# - Include routers: admin, field, mission
# - CORS open (hotspot LAN demo)
# - Mount nothing else
```

### `api/auth.py`

```python
async def current_user(x_bearer_token: str = Header(...)) -> dict:
    """FastAPI dependency. Looks up via api.db.users.get_user_by_token.
    Raises HTTPException(401) on miss. Returns full user dict."""

# Optionally an admin_user dependency that checks role='team_leader' or a
# static admin bearer from env (ADMIN_BEARER_TOKEN). Use env-only for demo:
# if X-Bearer-Token matches env ADMIN_BEARER_TOKEN, pass; else 401.
```

### `api/schemas.py`

Pydantic v2 models for all request/response bodies. Match spec §11 exactly.

### `api/routes/admin.py`

Per spec §11:

- `POST /admin/mission` — body validated by pydantic, calls
  `db.missions.create_mission`, then synchronously calls
  `scripts.fetch_terrain.fetch_terrain(mission_id)` and
  `scripts.seed_segments.seed_segments(mission_id)`, then
  `db.missions.set_status(mission_id, 'active')`, then
  `db.gate.enqueue_trigger(mission_id, 'mission_start')`. Returns
  `{mission_id, n_segments, n_terrain_cells}`.

  Synchronous orchestration is acceptable for hackathon — fetch_terrain
  takes ~30s with `--mock`. For real fetch, accept that the request may
  take longer; client should show a spinner.

- `POST /admin/users` — calls `db.users.create_user`, returns
  `{user_id, bearer_token, callsign}`.

### `api/routes/field.py`

- `POST /field/ping` — body `{lat, lon, ts?, accuracy_m?, speed_mps?, battery_pct?}`.
  Resolves user from bearer token. mission_id = `db.missions.active_mission_id()`.
  Calls `db.pings.insert_ping`. Returns `{ping_id}`. (Gate triggers
  `divergence`/`no_comms_recovery` are deferred to spatial worker, not
  fired here.)
- `GET /field/me` — stub returning `{user, active_dispatch: null, segment_geojson: null, nearby_hazards: [], recent_broadcasts: []}` for now. Full impl later.

### `api/routes/mission.py`

- `GET /mission/state.geojson?mission_id=N` — optional query param. If
  omitted, uses `db.missions.active_mission_id()`. Calls
  `db.geojson.mission_state_feature_collection`. Returns the FeatureCollection
  with `Content-Type: application/geo+json`.

---

## Test points

Each layer should be runnable end-to-end manually:

1. `./dev/reset-db.sh` — clean DB with migrations applied.
2. `python -c "from api.db.users import create_user; print(create_user('Dev','Alpha','searcher'))"` — DB layer smoke test.
3. `python scripts/fetch_terrain.py --mission-id 1 --mock` — pipeline smoke test (needs a mission row).
4. `./dev/run-api.sh` then `curl -X POST http://localhost:8000/admin/mission -H 'X-Bearer-Token: $ADMIN' -d @fixtures/mission_wilder.json` — full E2E.
