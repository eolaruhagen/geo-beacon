# Schema reference — `mission.db`

SQLite + SpatiaLite. Single file at `MISSION_DB_PATH` (default `/home/asus/sqlite/mission.db`, dev default `dev/data/mission.db`). 11 tables + `schema_migrations`.

**SpatiaLite must be loaded** before any geometry op. `api/db/__init__.py:connect` does it for you — use `from api.db import session` rather than `sqlite3.connect`. Pragmas it sets: `journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL`, `row_factory=sqlite3.Row`.

**Geometry gotcha:** SpatiaLite's `MakePoint(X, Y, SRID)` is `(lon, lat, 4326)`. Easy to flip.

**SRID:** every geometry column is WGS84 (4326).

**Migration runner:** `scripts/apply_migrations.py` runs on startup, reads `migrations/*.sql` in lex order, tracks applied files in `schema_migrations(name PK, applied_ts)`.

---

## `users`

One row per searcher / observer. Bearer-token auth.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `display_name` | TEXT | no | — | |
| `callsign` | TEXT | yes | — | UNIQUE globally; null for observers |
| `phone` | TEXT | yes | — | |
| `role` | TEXT | no | `'searcher'` | `searcher` \| `observer` |
| `status` | TEXT | no | `'standby'` | `standby` \| `dispatched` \| `on_segment` \| `returning` \| `no_comms` \| `off_duty` |
| `bearer_token` | TEXT | no | — | UNIQUE; 64-hex (`secrets.token_hex(32)`) |
| `created_ts` | INTEGER | no | — | unix epoch s |

## `missions`

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `name` | TEXT | no | — | |
| `status` | TEXT | no | — | `planning` \| `active` \| `subject_found` \| `suspended` \| `ended` |
| `subject_description` | TEXT | no | — | |
| `pls_lat` | REAL | no | — | Point Last Seen |
| `pls_lon` | REAL | no | — | |
| `pls_ts` | INTEGER | no | — | |
| `join_code` | TEXT | no | — | UNIQUE; 6-hex |
| `created_by_user_id` | INTEGER | no | — | FK `users.id`; this user = mission admin |
| `started_ts` | INTEGER | no | — | |
| `ended_ts` | INTEGER | yes | — | set when status → `ended` |
| `area_geom` | **POLYGON** | no | — | added in 002; spatial-indexed |

## `dispatches`

Agent's primary write surface. One row = one order to one searcher.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `mission_id` | INTEGER | no | — | FK `missions.id` |
| `user_id` | INTEGER | no | — | FK `users.id` (the searcher) |
| `segment_id` | INTEGER | **yes** | — | FK `segments.id`; NULL = recall / staging |
| `sweep_type` | TEXT | yes | — | `hasty` \| `efficient` \| `thorough` |
| `entry_lat` | REAL | yes | — | suggested entry point |
| `entry_lon` | REAL | yes | — | |
| `instruction` | TEXT | no | — | human-readable order |
| `reasoning` | TEXT | no | — | agent rationale |
| `status` | TEXT | no | — | `pending` \| `acked` \| `in_progress` \| `completed` \| `cancelled` \| `superseded` |
| `issued_ts` | INTEGER | no | — | |
| `acked_ts` | INTEGER | yes | — | |
| `started_ts` | INTEGER | yes | — | |
| `completed_ts` | INTEGER | yes | — | |
| `superseded_by` | INTEGER | yes | — | FK `dispatches.id` (self-ref) |

Indexes: `(user_id, status)`, `(mission_id, issued_ts DESC)`.

## `broadcasts`

Agent → app messages.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `mission_id` | INTEGER | no | — | FK `missions.id` |
| `scope` | TEXT | no | — | `'all'` or `'user:{id}'` (convention; no DB CHECK) |
| `kind` | TEXT | no | — | `info` \| `warning` \| `recall` \| `finding_alert` \| `route_correction` |
| `message` | TEXT | no | — | |
| `ts` | INTEGER | no | — | |

Indexes: `(mission_id, ts DESC)`.

## `pings`

Append-only GPS stream. `geom` written as `MakePoint(lon, lat, 4326)`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `user_id` | INTEGER | no | — | FK `users.id` |
| `mission_id` | INTEGER | no | — | FK `missions.id` |
| `ts` | INTEGER | no | — | |
| `lat` | REAL | no | — | duplicated in `geom` |
| `lon` | REAL | no | — | |
| `accuracy_m` | REAL | yes | — | |
| `speed_mps` | REAL | yes | — | |
| `battery_pct` | INTEGER | yes | — | |
| `source` | TEXT | no | — | `phone` \| `replay` \| `manual` |
| `geom` | **POINT** | no | — | spatial-indexed |

Indexes: `(user_id, ts)`, `(mission_id, ts)`, spatial on `geom`.

## `segments`

~100m search sectors. The agent reasons at this grain. Terrain stats aggregated from `hex_cells` at seed.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `mission_id` | INTEGER | no | — | FK `missions.id` |
| `name` | TEXT | no | — | "S-001"; UNIQUE per `(mission_id, name)` |
| `area_m2` | REAL | no | — | |
| `poa` | REAL | no | — | 0–1, ~sums to 1 |
| `pod` | REAL | no | `0` | hex-counting fraction |
| `pos` | REAL | no | `0` | `poa * pod`, denormalized |
| `status` | TEXT | no | — | `unassigned` \| `assigned` \| `in_progress` \| `swept` \| `cleared` |
| `assigned_user_id` | INTEGER | yes | — | FK `users.id` |
| `sweep_type` | TEXT | yes | — | `hasty` \| `efficient` \| `thorough` |
| `target_pod` | REAL | yes | — | 0.5 / 0.7 / 0.85 |
| `avg_slope_deg` | REAL | no | — | |
| `dominant_cover` | TEXT | no | — | `open` \| `mixed` \| `dense` \| `water` \| `rock` \| `built` |
| `trail_length_m` | REAL | no | `0` | |
| `geom` | **POLYGON** | no | — | spatial-indexed |

Indexes: `(mission_id, status)`, UNIQUE `(mission_id, name)`, spatial on `geom`.

## `findings`

Searcher-reported observations.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `mission_id` | INTEGER | no | — | FK `missions.id` |
| `reporter_user_id` | INTEGER | no | — | FK `users.id` |
| `hex_id` | INTEGER | no | — | FK `hex_cells.id` (forward ref, resolved by 003) |
| `ts` | INTEGER | no | — | |
| `lat` | REAL | no | — | |
| `lon` | REAL | no | — | |
| `kind` | TEXT | no | — | `clue` \| `subject_found` \| `subject_sighting` \| `hazard` \| `footprint` \| `discarded_item` \| `note` \| `other` |
| `description` | TEXT | no | — | |
| `confidence` | REAL | no | — | 0–1 |
| `photo_url` | TEXT | yes | — | reserved |
| `geom` | **POINT** | no | — | spatial-indexed |

Indexes: `(mission_id, ts DESC)`, `(hex_id)`, spatial on `geom`.

## `hazards`

All hazard polygons — both auto-derived structural (water/road buffer/building buffer/cliff) and runtime (weather, no-comms, wildlife). `hex_cells.flag_danger` is the rasterized fast-cache.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `mission_id` | INTEGER | no | — | FK `missions.id` |
| `kind` | TEXT | no | — | `cliff` \| `water` \| `weather` \| `no_comms_zone` \| `wildlife` \| `other` |
| `severity` | TEXT | no | — | `info` \| `caution` \| `critical` |
| `description` | TEXT | no | — | |
| `created_ts` | INTEGER | no | — | |
| `expires_ts` | INTEGER | yes | — | |
| `geom` | **POLYGON** | no | — | spatial-indexed; **POLYGON only**, no MultiPolygon |

Indexes: `(mission_id)`, spatial on `geom`.

## `hex_cells`

Fine-grained (~30m) coverage / terrain / runtime-flag grid. The agent never reads these directly — skill layer is the firewall.

Cells are currently axis-aligned **squares** (the seeder emits 4-vertex polygons), not regular hexagons. Schema is permissive (just `POLYGON`).

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `mission_id` | INTEGER | no | — | FK `missions.id` |
| `segment_id` | INTEGER | no | — | FK `segments.id` |
| `center_elev_m` | REAL | no | — | |
| `slope_deg` | REAL | no | — | |
| `dominant_cover` | TEXT | no | — | `open` \| `mixed` \| `dense` \| `water` \| `rock` \| `built` |
| `has_trail` | INTEGER | no | `0` | bool, set at seed |
| `has_road` | INTEGER | no | `0` | |
| `is_building` | INTEGER | no | `0` | |
| `is_water` | INTEGER | no | `0` | |
| `flag_danger` | INTEGER | no | `0` | rasterized from `hazards` |
| `flag_impassable` | INTEGER | no | `0` | runtime — agent/volunteer-reported |
| `flag_clue` | INTEGER | no | `0` | runtime — set when finding lands in this hex |
| `flag_poi` | INTEGER | no | `0` | runtime |
| `flags_updated_ts` | INTEGER | yes | — | |
| `geom` | **POLYGON** | no | — | spatial-indexed |

Indexes: `(mission_id)`, `(segment_id)`, partial indexes on each `flag_*` where `flag=1`, spatial on `geom`.

Derived "searchable" predicate: `is_building = 0 AND is_water = 0 AND flag_impassable = 0`. Not a stored column.

## `hex_visits`

Append-only log of (hex, user, ts). Source of truth for coverage; POD = `count distinct visited searchable hexes / total searchable hexes` per segment.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `hex_id` | INTEGER | no | — | FK `hex_cells.id` |
| `user_id` | INTEGER | no | — | FK `users.id` |
| `ts` | INTEGER | no | — | |

Indexes: `(hex_id)`, `(user_id, ts)`. **No UNIQUE on `(hex_id, user_id)`** — the spatial worker must dedupe.

## `osm_features`

Trails, roads, water polygons, building footprints. Mixed geometry types in one column.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | INTEGER | no | — | PK |
| `mission_id` | INTEGER | no | — | FK `missions.id` |
| `kind` | TEXT | no | — | `trail` \| `road` \| `water` \| `building` |
| `name` | TEXT | yes | — | from OSM `name=*` |
| `geom` | **GEOMETRY** | no | — | LINESTRING for trail/road, POLYGON for water/building |

Indexes: `(mission_id, kind)`, spatial on `geom`.

---

## DB helpers (Python)

Use these — don't write raw SQL in routes if a helper exists.

- `api.db.session()` — context manager, SpatiaLite-loaded connection.
- `api.db.users` — `create_user`, `get_user_by_token`, `get_user`.
- `api.db.missions` — `create_mission`, `get_mission`, `get_mission_by_join_code`, `set_status`, `active_mission_id_for_user`.
- `api.db.pings` — `insert_ping`.
- `api.db.segments` — `bulk_insert_segments`, `segments_for_mission`, `apply_hazard_penalty`.
- `api.db.hex_cells` — `bulk_insert_hex_cells`, `hex_cells_for_mission`, `rasterize_hazard_to_hex_flags`, `hex_cell_id_at`, `set_flag_clue_for_hex`.
- `api.db.osm` — `bulk_insert_osm_features`, `osm_features_for_mission`.
- `api.db.hazards` — `bulk_insert_hazards`, `hazards_for_mission`, `delete_hazards_for_mission`.
- `api.db.geojson` — `mission_state_feature_collection` (the big `/mission/state.geojson` aggregator).

No helpers yet for `dispatches`, `broadcasts`, `hex_visits` — write those when Phase 2/3 lands.
