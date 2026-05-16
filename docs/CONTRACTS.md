# Inter-layer contracts — hex-grid refactor

Pinned function signatures so the DB / pipeline / API layers can be built in
parallel. Anything not listed here is implementation detail and may change
freely inside the owning layer.

All timestamps are unix-epoch INTEGER seconds. All lat/lon are WGS84 floats.
SQLite connection is obtained via `from api.db import session` (SpatiaLite-loaded
context manager).

**Schema source of truth:** `migrations/001_init.sql`, `002_spatial.sql`,
`003_terrain.sql`. There is no `004_*`. Tables: `users`, `missions`,
`dispatches`, `broadcasts`, `pings`, `segments`, `findings`, `hazards`,
`hex_cells`, `hex_visits`, `osm_features`. No `agent_invocation_queue`, no
`agent_journal`, no `coverage_cache`, no `terrain_cells`.

**Hazard model (important):** the `hazards` table holds **all** hazard
polygons, including the structural ones we generate at init (water, road,
building, cliff). It is the polygon source-of-truth. `hex_cells.flag_danger` /
`.is_water` / `.is_building` are the **rasterized fast-cache** computed by
intersecting hazard polygons (and raw OSM features) against the hex grid. The
table comment in the migration is misleading — overruled.

**Init order on `POST /missions`:**
1. Create user (mission creator) — gets a bearer_token
2. Create mission with `created_by_user_id=user.id` and a random `join_code`
3. `fetch_terrain(mission_id)` — produces hex-cell terrain data in memory
   (DEM-derived slope + WorldCover dominant_cover for each hex centroid),
   inserts `osm_features` rows
4. `seed_segments(mission_id, hex_data)` — generates ~100m segment polygons,
   aggregates terrain stats from `hex_data`, inserts `segments`
5. `seed_hex_cells(mission_id, hex_data)` — assigns each hex to a segment via
   point-in-polygon, inserts `hex_cells` (with `is_water` / `is_building` /
   `has_trail` / `has_road` derived from OSM spatial intersection)
6. `seed_hazards(mission_id)` — derives structural hazards from `osm_features`
   (water → critical, road buffered → caution, building buffered → caution)
   and from `hex_cells` (slope ≥ 30° → cliff caution). Inserts into `hazards`,
   then **rasterizes**: for each hazard polygon, sets `flag_danger=1` on every
   `hex_cells` it intersects.
7. Optional: any `hazards` payload from the request body is appended in
   `seed_hazards` and rasterized the same way.
8. `set_status(mission_id, 'active')`

(Agent loop is cron-driven — no queue table to enqueue into.)

---

## Layer 1 — DB helpers (`api/db/`)

Pure DB ops. No FastAPI imports, no HTTP, no geospatial math beyond SpatiaLite SQL.

### `api/db/missions.py`

```python
def create_mission(
    name: str,
    subject_description: str,
    pls_lat: float,
    pls_lon: float,
    pls_ts: int,
    area_geojson: dict,
    created_by_user_id: int,
    join_code: str,
) -> int:
    """Insert mission row. status='planning'. started_ts=now. Returns mission_id."""

def get_mission(mission_id: int) -> dict | None:
    """All columns plus area_geom as GeoJSON dict (key 'area_geojson')."""

def get_mission_by_join_code(join_code: str) -> dict | None: ...

def set_status(mission_id: int, status: str) -> None: ...

def active_mission_id_for_user(user_id: int) -> int | None:
    """Returns mission_id of the most recent mission this user is associated
    with (created or joined via a ping). For the single-active-mission
    hackathon scope this is also effectively `the active mission`."""
```

### `api/db/users.py`

```python
def create_user(
    display_name: str,
    callsign: str | None,
    role: str = "searcher",   # 'searcher' | 'observer' — no 'team_leader'
) -> dict:
    """Inserts user with status='standby', random hex bearer_token (32 bytes).
    Returns {id, display_name, callsign, role, status, bearer_token, created_ts}."""

def get_user_by_token(token: str) -> dict | None: ...
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
    source: str = "phone",
) -> int: ...
```

### `api/db/segments.py`

```python
SegmentRow = dict   # keys: name, poly_geojson, area_m2, poa, avg_slope_deg,
                    #       dominant_cover, trail_length_m

def bulk_insert_segments(mission_id: int, rows: list[SegmentRow]) -> list[int]:
    """Insert all rows in one transaction. status='unassigned'.
    Returns list of inserted ids in the same order as input rows
    (callers need the ids to set hex_cells.segment_id)."""

def segments_for_mission(mission_id: int) -> list[dict]: ...

def apply_hazard_penalty(mission_id: int) -> dict[str, int]:
    """For segments intersecting hazards: critical → POA × 0, caution → × 0.3.
    Then renormalize Σ poa = 1. Returns {critical_zeroed, caution_penalized}."""
```

### `api/db/hex_cells.py` (replaces the old `api/db/terrain.py`)

```python
HexCellRow = dict   # keys: poly_geojson, segment_id, center_elev_m, slope_deg,
                    #       dominant_cover, has_trail, has_road, is_building,
                    #       is_water

def bulk_insert_hex_cells(mission_id: int, rows: list[HexCellRow]) -> int: ...

def hex_cells_for_mission(mission_id: int) -> list[dict]:
    """Includes geom as GeoJSON dict (key 'poly_geojson') and all flag columns."""

def rasterize_hazard_to_hex_flags(mission_id: int, hazard_id: int) -> int:
    """For one hazard row: UPDATE hex_cells SET flag_danger=1 WHERE the hex
    geom ST_Intersects this hazard's geom AND mission_id matches.
    Returns count of hex_cells flagged."""

def hex_cell_id_at(mission_id: int, lat: float, lon: float) -> int | None:
    """Point-in-polygon lookup. Used by /field/findings to resolve hex_id
    when caller provides lat/lon, and the reverse via centroid when caller
    provides hex_id."""

def set_flag_clue_for_hex(hex_id: int) -> None:
    """Sets flag_clue=1, updates flags_updated_ts."""

# OSM features stay in their own helper module — see api/db/osm.py below.
```

### `api/db/osm.py` (split out from old terrain.py)

```python
OSMFeature = dict   # keys: kind ('trail'|'road'|'water'|'building'),
                    #       name (optional), geom_geojson (Polygon or LineString)

def bulk_insert_osm_features(mission_id: int, features: list[OSMFeature]) -> int: ...
def osm_features_for_mission(mission_id: int) -> list[dict]: ...
```

### `api/db/hazards.py` (mostly unchanged — keep current signatures)

```python
def bulk_insert_hazards(mission_id: int, hazards: list[dict]) -> list[int]:
    """Returns list of inserted hazard ids in order so callers can iterate
    them for rasterization."""

def hazards_for_mission(mission_id: int) -> list[dict]: ...
def delete_hazards_for_mission(mission_id: int) -> int: ...
```

### `api/db/geojson.py` (extend, keep existing layers)

```python
def mission_state_feature_collection(mission_id: int) -> dict:
    """Per spec §11. Returns Features for:
      - segments (Polygon, props: id, name, poa, pod, pos, status, sweep_type, assigned_user_id)
      - hex_cells with non-default flags only (Polygon, props: id, flag_danger,
        flag_impassable, flag_clue, flag_poi, is_water, is_building)
      - searchers (Point, latest ping per user)
      - tracks (LineString, last 30 min per searcher)
      - findings (Point)
      - hazards (Polygon, props: id, kind, severity, description)
      - osm_features (LineString/Polygon, props: kind, name)"""
```

### REMOVED

- `api/db/gate.py` — delete. No queue table; agent is cron-driven.
- `api/db/terrain.py` — delete (replaced by `hex_cells.py` + `osm.py`).

---

## Layer 2 — Pipelines

### `scripts/fetch_terrain.py`

```python
def fetch_terrain(mission_id: int, mock: bool | None = None) -> dict:
    """Reads mission.area_geom. Computes a ~30m hex grid covering the bbox.
    For each hex centroid: queries DEM for elevation + slope, queries
    WorldCover for dominant_cover. Inserts osm_features (DB write).

    Does NOT insert hex_cells yet — returns them in memory so seed_segments
    can aggregate terrain stats and assign segment_ids before insert.

    `mock` defaults to env TERRAIN_MOCK=1 → True, else False. Real path
    falls back to mock on network failure (5xx, timeout, 406).

    Returns:
      {
        "osm_features_inserted": int,
        "hex_data": list[dict],   # each: {center_lat, center_lon, poly_geojson,
                                    #        center_elev_m, slope_deg, dominant_cover,
                                    #        has_trail, has_road, is_building, is_water}
      }"""
```

Mock path: synthetic hex grid (~5000 cells for 2km×2km), one trail / one road /
one water polygon as before. Real path: USGS NED via Open-Elevation,
WorldCover, OSM Overpass. Reuse the User-Agent / timeout fix.

### `scripts/seed_segments.py`

```python
def seed_segments(mission_id: int, hex_data: list[dict]) -> list[int]:
    """Subdivides mission area into ~100m segment polygons. For each segment,
    aggregates terrain stats from the hex_data points that fall inside it
    (avg_slope_deg = mean, dominant_cover = mode, trail_length_m = sum from
    has_trail hexes × 30m). Computes POA per spec §7 using agent/poa.py.

    Bulk-inserts via api.db.segments.bulk_insert_segments.
    Returns the list of inserted segment ids (for the caller to use
    when assigning hex_cells.segment_id)."""
```

### `scripts/seed_hex_cells.py` (new)

```python
def seed_hex_cells(mission_id: int, hex_data: list[dict],
                   segment_ids: list[int]) -> int:
    """For each hex in hex_data, point-in-polygon lookup against segments
    (using SpatiaLite ST_Contains via the inserted segment ids) to find
    its segment_id. Bulk-inserts hex_cells.

    Hexes that fall outside any segment polygon are dropped (this is the
    edge-of-bbox case — segments are clipped to the mission area).

    Returns count inserted."""
```

### `scripts/seed_hazards.py`

```python
def seed_hazards(mission_id: int) -> dict[str, int]:
    """Inserts structural hazards into the hazards table, then rasterizes:
    for each new hazard row, sets flag_danger=1 on intersecting hex_cells.

    Sources:
      - osm_features.kind='water'    → hazard kind='water', severity='critical'
      - osm_features.kind='road'     → kind='other', 'caution', buffered 5m
      - osm_features.kind='building' → kind='other', 'caution', buffered 2m
      - hex_cells.slope_deg ≥ 30     → kind='cliff', 'caution' (one row per
                                       connected component, falling back to
                                       per-cell if connected-component is hard)

    Also sets is_water=1 on hex_cells inside water osm_features, and
    is_building=1 on hex_cells inside building osm_features (NOT via hazard
    rasterization — these reflect the underlying feature type, not just
    the danger annotation).

    Returns counts {water, road, building, cliff, total_hazards,
                    hexes_flagged_danger, hexes_flagged_water, hexes_flagged_building}.

    Idempotent: delete_hazards_for_mission first, also reset relevant
    hex_cells flags (flag_danger=0, is_water=0, is_building=0) for this
    mission, then re-derive.

    Body-param hazards from POST /missions are appended via a separate
    call to bulk_insert_hazards + rasterize after this returns."""
```

### `agent/poa.py` (unchanged)

---

## Layer 3 — FastAPI routes

### `api/main.py`

- FastAPI app, CORS open
- Startup hook runs `apply_migrations`
- Includes routers: `missions`, `field`, `mission`, `admin`

### `api/auth.py`

```python
async def current_user(x_bearer_token: str = Header(...)) -> dict:
    """Looks up via api.db.users.get_user_by_token. 401 on miss."""

async def admin_for_mission(mission_id: int, user: dict = Depends(current_user)) -> dict:
    """403 unless user.id == missions.created_by_user_id."""
```

(No more env-var ADMIN_BEARER_TOKEN. Admin = mission creator.)

### `api/schemas.py`

Pydantic v2 for all bodies. Validators: lat in [-90,90], lon in [-180,180],
confidence in [0,1], role in {searcher, observer}, hazard kind/severity per
the migration CHECK constraints.

### `api/routes/missions.py` (replaces `admin.py`)

```python
POST /missions
  body: {
    name, subject_description, pls_lat, pls_lon, pls_ts, area_geojson,
    display_name, callsign?, hazards?   # hazards = optional list of
                                        # {kind, severity, description, poly_geojson}
  }
  flow:
    user = db_users.create_user(display_name, callsign, "searcher")
    mission_id = db_missions.create_mission(..., created_by_user_id=user.id,
                                            join_code=randint())
    terrain = fetch_terrain(mission_id)
    segment_ids = seed_segments(mission_id, terrain["hex_data"])
    n_hex = seed_hex_cells(mission_id, terrain["hex_data"], segment_ids)
    hazard_counts = seed_hazards(mission_id)
    if body.hazards:
        ids = bulk_insert_hazards(mission_id, body.hazards)
        for h_id in ids:
            rasterize_hazard_to_hex_flags(mission_id, h_id)
    apply_hazard_penalty(mission_id)
    set_status(mission_id, "active")
  returns: {mission_id, join_code, bearer_token, user_id, n_segments,
            n_hex_cells, n_hazards}

POST /missions/join
  body: {join_code, display_name, callsign?, role?}
  flow:
    mission = get_mission_by_join_code(join_code)  # 404 if not found
    user = create_user(display_name, callsign, role or "searcher")
  returns: {mission_id, bearer_token, user_id, callsign}
```

### `api/routes/field.py`

- `POST /field/ping` — auth via bearer, mission_id resolved via
  `active_mission_id_for_user(user.id)`. Same as before.
- `GET /field/me` — same stub for now; `nearby_hazards` query is a follow-up.
- `POST /field/findings` — new: accepts `{lat, lon, kind, description, confidence}`
  OR `{hex_id, ...}`. Server resolves the other. Sets `hex_cells.flag_clue=1`.

### `api/routes/mission.py`

- `GET /mission/state.geojson?mission_id=N` — calls
  `db.geojson.mission_state_feature_collection`. New features include
  `hex_cells` (filtered to non-default flags) + `osm_features`.

### `api/routes/admin.py` (smaller now)

- `POST /admin/agent/invoke` — stub for now, returns 501 or no-op.
- `POST /admin/mission/{id}/finish` — sets status='ended'. Uses
  `admin_for_mission` dependency.

---

## Out of scope for THIS refactor

- Hex-counting POD math (needs spatial worker; agent loop work)
- `GET /field/me/route` snap-to-trail
- `GET /mission/timeline` event feed
- Agent invocation (cron skeleton)
- Replay worker

User's instruction: "no need to be perfect, just have the proper data shapes
aligned, if slight bugs ship its ok, ill fix them up as they integrate".
