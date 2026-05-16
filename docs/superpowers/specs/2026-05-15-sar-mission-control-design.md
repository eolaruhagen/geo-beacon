# SAR Mission Control — Design Spec

**Status:** Schema locked **Date:** 2026-05-15 (rev 2026-05-16) **Hackathon budget:** 20 hours **Pivoted from:** personal life-pattern brief generator (see `Hack-a-Claw.md`)

---

## 1. Mission

Build an **autonomous AI mission commander** for land search-and-rescue operations.

The agent (openclaw on an NVIDIA DGX Spark) ingests:

- live searcher GPS tracks (via a thin mobile app),
- public terrain data (DEM-derived slope, ESA WorldCover landcover, OSM trails/roads/water),
- field-reported findings (clues, sightings, hazards, subject-found),

reasons about coverage, probability, and safety, and **issues structured dispatches to individual searchers**. Each searcher's app polls FastAPI for updates and renders their current orders, segment polygon, hazards, and other searchers' status.

Mission Control **is** the AI. No human dispatcher in the loop. A commander web dashboard exists for the demo audience to watch, not interact.

## 2. Scope (20-hour cut)

**In:**

- Single active mission at a time
- 4–8 searchers, each dispatched individually (no team layer)
- Bounded search area (~2 km × 2 km) with pre-fetched terrain data
- App for each searcher: status, assignment, map, findings logging, SOS
- FastAPI on the DGX as the orchestration layer
- SQLite + SpatiaLite for state, persisted at `/home/asus/sqlite/mission.db`
- openclaw invoked by a polling agent worker (no queue), emits structured tool calls that write to DB
- Replay/sim worker for demo determinism (plus hybrid mode with real teammates)
- Read-only commander web dashboard (Leaflet + polling)
- Static hazard rasterization at mission init time

**Out (explicitly deferred):**

- Multiple concurrent missions
- ICS organizational hierarchy (we collapse to "agent is commander")
- Teams as a first-class concept — dispatches target individual users
- Real authentication (per-user bearer token, plaintext fine for hack)
- Runtime hazard flagging UI (hex-tap to mark danger/impassable/POI from the phone) — data model supports it, UI is post-hack
- Photo / audio findings (text + GPS + kind only)
- Offline buffering on the phone
- Map-matched routing (snap-to-nearest-trail only, no graph routing)
- WhatsApp / SMS integration (deferred to v2 — app-only this round)
- K9 / drone / aerial-asset special handling
- Battery / fatigue management
- Indoor / multi-floor search modeling
- Anything that doesn't directly enable the demo narrative

## 3. Personas

- **Searcher**: ground volunteer with a phone running the app. Receives assignment from agent, follows it, logs findings, reports complete.
- **Mission creator**: the searcher who created the mission. Implicitly the "admin" — can force agent invocations and end the mission. Otherwise identical to any other searcher.
- **Agent (openclaw)**: assumes the combined role of Incident Commander + Planning Chief + Operations Chief in real-SAR ICS terms. Decides dispatches, revises POA on new evidence, recalls searchers, flags hazards.
- **Observer (judge / demo viewer)**: watches Mission Control web dashboard during demo; doesn't interact.

## 4. Architecture overview

```
┌────────────────────────────────────────────────────────────────────┐
│  FIELD TIER                                                        │
│  4–8 searchers, each with phone running app                        │
│   • POST /field/ping (every 30s, background or manual)             │
│   • GET  /field/me   (every 5s when app open)                      │
│   • GET  /mission/state.geojson (every 10s when map tab open)      │
│   • POST /field/findings, /field/dispatch/{id}/{ack|start|done}    │
│  Replay path: replay_worker on DGX hits the SAME endpoints         │
└───────────────────────────┬────────────────────────────────────────┘
                            │ HTTPS through ngrok tunnel
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│  FASTAPI on DGX (orchestrator)                                     │
│   routes/missions.py  — create / join                              │
│   routes/field.py     — searcher writes + me-state reads           │
│   routes/mission.py   — geojson state, timeline, agent journal     │
│   routes/admin.py     — force-invoke, finish (mission creator only)│
└───────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│  SQLite + SpatiaLite at /home/asus/sqlite/mission.db (WAL mode)    │
│  Tables: users, missions, segments, pings, dispatches, findings,   │
│  hazards, broadcasts, hex_cells, hex_visits, osm_features,         │
│  agent_journal                                                     │
└───────────────────────────┬────────────────────────────────────────┘
                            │
   ┌────────────────────────┴─────────────────────────────────┐
   │  Worker processes (tmux, sleep-loop pattern):            │
   │                                                          │
   │  • spatial_worker  (every 15s)                           │
   │      new pings → point-in-hex → INSERT INTO hex_visits   │
   │      → recompute segments.pod from visited hex counts    │
   │      → mark swept, detect divergence, regenerate brief   │
   │                                                          │
   │  • agent_worker    (polls every ~15s, no queue)          │
   │      diff events since missions.last_agent_invocation_ts │
   │      → if events OR force_agent_invoke=1 OR >60s idle:   │
   │        load Mission Brief → call openclaw →              │
   │        execute tool calls → write journal entry          │
   │                                                          │
   │  • replay_worker   (sim mode only)                       │
   │      reads recordings/*.jsonl → injects events on        │
   │      schedule via /field endpoints                       │
   └──────────────────────────────────────────────────────────┘

                            ▼
┌────────────────────────────────────────────────────────────────────┐
│  MISSION CONTROL WEB DASHBOARD (Leaflet, polls 3s)                 │
│  Big map + agent journal sidebar + timeline + searcher status panel│
│  Hosted by FastAPI at /mission/dashboard                           │
└────────────────────────────────────────────────────────────────────┘
```

**Network reality:** laptops + DGX share a phone-hotspot SSID for dev/SSH. Phones reach FastAPI through an ngrok tunnel — they're not required to be on the hotspot. App reads the ngrok URL from env / a constants file since the URL changes when the tunnel restarts. Deploy is `./scripts/dgx.sh 'cd ~/geo-beacon && git pull && ./scripts/respawn-workers.sh'`.

## 5. Data model

All geometry columns are SpatiaLite-managed; create with `AddGeometryColumn` and `CreateSpatialIndex`. Migration files in `migrations/` are the authoritative schema; this section is a narrative companion.

### Three layers, by purpose

| Layer | Tables | Role |
|---|---|---|
| **Identity / coordination** | users, missions, dispatches, broadcasts | Who is playing, what mission, who's been told what |
| **Spatial / agent-facing** | segments, pings, findings, hazards | What the world looks like at the agent's reasoning grain (~100m segments) |
| **Hex infrastructure / rendering** | hex_cells, hex_visits, osm_features | Fine-grained (~30m) cells for coverage tracking, OSM rasterization, runtime flags |

**The agent reasons only at the segment level.** Hexes are infrastructure the agent never sees — coverage tracking, render granularity, runtime annotation. The skill layer in `agent/skills/` is the firewall between hex-grain infrastructure and segment-grain agent reasoning.

### `users`

Each searcher is a user. Callsigns live here directly.

```
id            INTEGER PRIMARY KEY
display_name  TEXT NOT NULL
callsign      TEXT UNIQUE                  -- 'Alpha', 'Bravo', ...; null for observers
phone         TEXT                         -- for future Twilio integration
role          TEXT NOT NULL DEFAULT 'searcher'  -- 'searcher' | 'observer'
status        TEXT NOT NULL DEFAULT 'standby'   -- 'standby' | 'dispatched' | 'on_segment' | 'returning' | 'no_comms' | 'off_duty'
bearer_token  TEXT UNIQUE NOT NULL         -- random hex, generated at join time
created_ts    INTEGER NOT NULL
```

### `missions`

```
id                       INTEGER PRIMARY KEY
name                     TEXT NOT NULL
status                   TEXT NOT NULL          -- 'planning' | 'active' | 'subject_found' | 'suspended' | 'ended'
subject_description      TEXT NOT NULL          -- "12yo male hiker in red jacket, last seen 14:20"
pls_lat                  REAL NOT NULL          -- Point Last Seen
pls_lon                  REAL NOT NULL
pls_ts                   INTEGER NOT NULL
area_geom                BLOB NOT NULL          -- POLYGON, bounding search area
join_code                TEXT UNIQUE NOT NULL   -- short shareable string (6-char base32 fine)
created_by_user_id       INTEGER NOT NULL REFERENCES users(id)  -- = "admin"
started_ts               INTEGER NOT NULL
ended_ts                 INTEGER
last_agent_invocation_ts INTEGER NOT NULL DEFAULT 0  -- agent_worker watermark
force_agent_invoke       INTEGER NOT NULL DEFAULT 0  -- 1 = next tick must invoke
```

### `segments`

The search sectors — the **dispatchable unit** and the **only grid the agent reasons about**. Polygon geometry; POA assigned at start, revised on findings. Terrain summary columns aggregated from `hex_cells` at seed.

```
id                INTEGER PRIMARY KEY
mission_id        INTEGER NOT NULL REFERENCES missions(id)
name              TEXT NOT NULL          -- 'S-01', 'S-02', ...
geom              BLOB NOT NULL          -- POLYGON
area_m2           REAL NOT NULL
poa               REAL NOT NULL          -- 0.0–1.0, sums to ~1.0 across mission
pod               REAL NOT NULL DEFAULT 0
pos               REAL NOT NULL DEFAULT 0   -- poa * pod, denormalized for sort
status            TEXT NOT NULL          -- 'unassigned' | 'assigned' | 'in_progress' | 'swept' | 'cleared'
assigned_user_id  INTEGER REFERENCES users(id)
sweep_type        TEXT                   -- 'hasty' | 'efficient' | 'thorough'
target_pod        REAL                   -- 0.5 / 0.7 / 0.85 per sweep type
avg_slope_deg     REAL NOT NULL          -- aggregated from hex_cells at seed
dominant_cover    TEXT NOT NULL          -- 'open' | 'mixed' | 'dense' | 'water' | 'rock' | 'built'
trail_length_m    REAL NOT NULL DEFAULT 0
UNIQUE (mission_id, name)
SpatialIndex on geom
```

### `pings`

Raw GPS pings; append-only source of truth.

```
id            INTEGER PRIMARY KEY
user_id       INTEGER NOT NULL REFERENCES users(id)
mission_id    INTEGER NOT NULL REFERENCES missions(id)
ts            INTEGER NOT NULL
lat           REAL NOT NULL
lon           REAL NOT NULL
geom          BLOB NOT NULL          -- POINT
accuracy_m    REAL
speed_mps     REAL
battery_pct   INTEGER
source        TEXT NOT NULL          -- 'phone' | 'replay' | 'manual'
INDEX (user_id, ts)
INDEX (mission_id, ts)
SpatialIndex on geom
```

### `dispatches`

The agent's primary write surface. One row = one order, targeting a single searcher.

```
id                INTEGER PRIMARY KEY
mission_id        INTEGER NOT NULL REFERENCES missions(id)
user_id           INTEGER NOT NULL REFERENCES users(id)
segment_id        INTEGER REFERENCES segments(id)  -- NULL for recall / staging move
sweep_type        TEXT                              -- NULL if not a search dispatch
entry_lat         REAL                              -- suggested entry point
entry_lon         REAL
instruction       TEXT NOT NULL                     -- human-readable; shown in app
reasoning         TEXT NOT NULL                     -- agent rationale, also shown
status            TEXT NOT NULL                     -- 'pending' | 'acked' | 'in_progress' | 'completed' | 'cancelled' | 'superseded'
issued_ts         INTEGER NOT NULL
acked_ts          INTEGER
started_ts        INTEGER
completed_ts      INTEGER
superseded_by     INTEGER REFERENCES dispatches(id)
INDEX (user_id, status)
INDEX (mission_id, issued_ts DESC)
```

### `findings`

Reported by searchers via the app. Carries a `hex_id` FK alongside `lat/lon/geom` — the FK makes "findings in this hex" cheap; the point geometry keeps the POA Gaussian bump accurate. Single server-side insert path resolves whichever the caller didn't provide.

```
id                INTEGER PRIMARY KEY
mission_id        INTEGER NOT NULL REFERENCES missions(id)
reporter_user_id  INTEGER NOT NULL REFERENCES users(id)
hex_id            INTEGER NOT NULL REFERENCES hex_cells(id)   -- forward ref, resolved in migration 003
ts                INTEGER NOT NULL
lat               REAL NOT NULL
lon               REAL NOT NULL
geom              BLOB NOT NULL          -- POINT
kind              TEXT NOT NULL          -- 'clue' | 'subject_found' | 'subject_sighting' | 'hazard'
                                         -- | 'footprint' | 'discarded_item' | 'note' | 'other'
description       TEXT NOT NULL
confidence        REAL NOT NULL          -- 0.0–1.0, self-assessed
photo_url         TEXT                   -- deferred for hack
INDEX (mission_id, ts DESC)
INDEX (hex_id)
SpatialIndex on geom
```

### `hazards`

Runtime metadata-rich dangers (weather, no-comms zones, wildlife sightings, volunteer-reported obstacles). Polygon geometry. **For the 20-hour cut, hazards are populated statically at mission init only** — runtime hex-tap flagging UI is a follow-up.

Seed-time terrain hazards (cliffs, buildings, water) do **not** go in this table — they live as flags directly on `hex_cells` (`is_building`, `is_water`, `flag_impassable`). The hazards table is for things that need description, expiration, or severity nuance beyond what a boolean can carry.

```
id              INTEGER PRIMARY KEY
mission_id      INTEGER NOT NULL REFERENCES missions(id)
geom            BLOB NOT NULL          -- POLYGON or buffered POINT
kind            TEXT NOT NULL          -- 'cliff' | 'water' | 'weather' | 'no_comms_zone' | 'wildlife' | 'other'
severity        TEXT NOT NULL          -- 'info' | 'caution' | 'critical'
description     TEXT NOT NULL
created_ts      INTEGER NOT NULL
expires_ts      INTEGER
SpatialIndex on geom
```

### `broadcasts`

Agent → app messages, scoped to all-hands or a single user.

```
id            INTEGER PRIMARY KEY
mission_id    INTEGER NOT NULL REFERENCES missions(id)
scope         TEXT NOT NULL          -- 'all' | 'user:{id}'
kind          TEXT NOT NULL          -- 'info' | 'warning' | 'recall' | 'finding_alert' | 'route_correction'
message       TEXT NOT NULL
ts            INTEGER NOT NULL
INDEX (mission_id, ts DESC)
```

### `agent_journal`

One row per agent invocation, for transparency. `trigger` is a comma-separated list of event kinds since the last invocation watermark (since there's no queue, an invocation can react to multiple events at once).

```
id              INTEGER PRIMARY KEY
mission_id      INTEGER NOT NULL REFERENCES missions(id)
ts              INTEGER NOT NULL
trigger         TEXT NOT NULL
brief_md        TEXT NOT NULL          -- snapshot of Mission Brief input
tool_calls_json TEXT NOT NULL          -- array of {tool, args, result}
reasoning       TEXT                   -- agent's narration if returned
duration_ms     INTEGER NOT NULL
INDEX (mission_id, ts DESC)
```

### `hex_cells`

The fine-grained grid (~30m corner-to-corner, ~5,000 cells per 2km × 2km mission). Replaces the original `terrain_cells` table — terrain truth lives here at finer resolution.

Each hex carries three classes of state:

1. **Immutable terrain truth** (set by `fetch_terrain.py` at seed) — `center_elev_m`, `slope_deg`, `dominant_cover`
2. **OSM-rasterized booleans** (set at seed via spatial join against `osm_features`) — `has_trail`, `has_road`, `is_building`, `is_water`
3. **Runtime flags** (set by skill layer on hazard/finding writes) — `flag_danger`, `flag_impassable`, `flag_clue`, `flag_poi`

`searchable` is a derived query: `NOT is_building AND NOT is_water AND NOT flag_impassable`. We don't store it as a column because keeping a derived bool in sync with three inputs is race-prone.

```
id              INTEGER PRIMARY KEY
mission_id      INTEGER NOT NULL REFERENCES missions(id)
segment_id      INTEGER NOT NULL REFERENCES segments(id)
geom            BLOB NOT NULL          -- POLYGON (6 vertices, regular hex)
center_elev_m   REAL NOT NULL
slope_deg       REAL NOT NULL
dominant_cover  TEXT NOT NULL          -- 'open' | 'mixed' | 'dense' | 'water' | 'rock' | 'built'
has_trail        INTEGER NOT NULL DEFAULT 0
has_road         INTEGER NOT NULL DEFAULT 0
is_building      INTEGER NOT NULL DEFAULT 0
is_water         INTEGER NOT NULL DEFAULT 0
flag_danger       INTEGER NOT NULL DEFAULT 0    -- any active hazard intersects this hex
flag_impassable   INTEGER NOT NULL DEFAULT 0    -- volunteer- or agent-reported obstacle
flag_clue         INTEGER NOT NULL DEFAULT 0    -- a finding exists in this hex
flag_poi          INTEGER NOT NULL DEFAULT 0
flags_updated_ts  INTEGER
SpatialIndex on geom
INDEX (mission_id), INDEX (segment_id)
Partial indices on each flag where flag=1
```

### `hex_visits`

Append-only log of (hex, user, ts). Source of truth for coverage.

```
id        INTEGER PRIMARY KEY
hex_id    INTEGER NOT NULL REFERENCES hex_cells(id)
user_id   INTEGER NOT NULL REFERENCES users(id)
ts        INTEGER NOT NULL
INDEX (hex_id)
INDEX (user_id, ts)
```

Spatial worker tick: for each new ping since last_tick, point-in-hex lookup, `INSERT OR IGNORE` one row per `(hex_id, user_id)` per ping. Recompute `segments.pod` from the count of distinct visited hexes per segment.

### `osm_features`

Trails, roads, water bodies, building footprints — for route hints (needs original line geometry for `ST_ClosestPoint`), map base layer (centerlines look right, hex ribbons don't), and seed-time rasterization input. Kept as a separate table even though `hex_cells` carries rasterized booleans.

```
id              INTEGER PRIMARY KEY
mission_id      INTEGER NOT NULL REFERENCES missions(id)
kind            TEXT NOT NULL          -- 'trail' | 'road' | 'water' | 'building'
name            TEXT
geom            BLOB NOT NULL          -- LINESTRING or POLYGON (stored as generic GEOMETRY)
SpatialIndex on geom
INDEX (mission_id, kind)
```

### What's NOT in the schema (and why)

- **No `teams` / `team_members`.** Dispatches target individual users via `dispatches.user_id` and `segments.assigned_user_id`. Searchers self-organize in the field.
- **No `agent_invocation_queue`.** The agent worker polls on a tick and diffs events against `missions.last_agent_invocation_ts`. High-priority triggers set `missions.force_agent_invoke = 1` to short-circuit the next sleep.
- **No `coverage_cache`.** POD is computed inline from `hex_visits` and written to `segments.pod` directly; no separate materialized cache needed.
- **No `terrain_cells`.** Folded into `hex_cells` at higher resolution.
- **No admin role.** Mission creator = admin via `missions.created_by_user_id`. Admin endpoints check `current_user.id == mission.created_by_user_id`.

## 6. Public map data integration

Pre-fetched once per mission area by `scripts/fetch_terrain.py`, given a bounding box. Output goes into `hex_cells` (terrain + rasterized OSM booleans) and `osm_features` (original geometries).

1. **USGS NED 1/3 arc-second DEM** → GeoTIFF for area. Use `rasterio` + `numpy` to derive slope raster: `slope = arctan(magnitude(gradient(elev)))`. For each hex cell, sample the DEM at the centroid for `center_elev_m` and average the slope over the underlying pixels for `slope_deg`.
2. **ESA WorldCover 2021** (10 m) → classify each hex by dominant underlying class. Map their classes to our 6 buckets: `open` (grassland, cropland, bare), `mixed` (shrubland, sparse tree), `dense` (closed forest), `water`, `rock` (snow/ice/bare rock), `built` (built-up).
3. **OSM via Overpass API**: pull `highway in (path, footway, track)` as `trail`; `highway in (primary, secondary, tertiary, residential, service, pedestrian)` as `road`; `natural=water`, `waterway=stream|river` as `water`; `building=*` as `building`. Insert each as an `osm_features` row.
4. **Rasterize OSM into hex flag columns**: for each hex, spatially intersect against `osm_features` and set the relevant boolean (`has_trail`, `has_road`, `is_building`, `is_water`).
5. **Aggregate to segments**: at seed, after hex_cells is populated and segments are created, denormalize `segments.avg_slope_deg`, `segments.dominant_cover`, and `segments.trail_length_m` by aggregating across each segment's hexes.

For demo: pre-fetch **Wilder Ranch State Park** (Santa Cruz, CA) — real elevation variation, real trails, accessible to demo presenter. Bbox roughly 36.95°N–37.00°N, -122.10°W–-122.05°W.

## 7. POD / POA math

Simplified from full Koopman to hex-counting; same POA shape.

### Initial POA assignment

At mission seed time, given PLS and mission area:

1. Subdivide area into ~100 m × 100 m square `segments`.
2. For each segment, compute raw weight from segment centroid:
   ```
   d = distance(segment_center, pls)
   dist_term      = exp(-d² / (2 · σ²))    where σ = 750 m
   trail_term     = 1.5 if any constituent hex has has_trail=1 else 1.0
   downhill_term  = 1.0 + 0.002 · max(0, pls_elev - segment_center_elev)
   cover_term     = 0.7 if dominant_cover ∈ {'dense','built'} else 1.0
   raw_w = dist_term · trail_term · downhill_term · cover_term
   ```
3. Normalize: `poa[i] = raw_w[i] / Σ raw_w`

Boosts are documented lost-person heuristics from ISRID-style literature, simplified.

### POD per segment (hex-counting)

POD is the fraction of searchable hexes in the segment that have been visited:

```
searchable_hexes_in_segment  = COUNT(hex_cells WHERE segment_id=S
                                                AND is_building=0 AND is_water=0
                                                AND flag_impassable=0)
visited_searchable           = COUNT(DISTINCT hex_id FROM hex_visits v
                                     JOIN hex_cells h ON h.id = v.hex_id
                                     WHERE h.segment_id=S AND searchable)
POD = visited_searchable / searchable_hexes_in_segment
```

No `ST_Buffer`, no `ST_Union`, no exponential. The spatial worker recomputes POD per segment each tick after inserting new hex_visits.

### POS

```
POS = POA · POD          # per segment
mission_POS = Σ POS      # cumulative; primary success metric for demo
```

### Sweep-complete threshold

```
target_pod = { hasty: 0.5, efficient: 0.7, thorough: 0.85 }
```

When `pod ≥ target_pod` for an in-progress segment, the spatial worker marks it `swept`, frees the assigned searcher (`segments.assigned_user_id = NULL`, `users.status = standby`), and counts as a `segment_swept` event for the next agent tick.

### POA revision on findings

When a finding is logged at `(f_lat, f_lon)` with confidence `c`:

1. Gaussian bump centered on finding, σ = 300 m, magnitude `0.4 · c` of total prior POA.
2. Add bump to each segment's POA in proportion to overlap area.
3. Subtract proportionally from all segments currently marked `swept`.
4. Renormalize so Σ POA = 1.

Always logged in `agent_journal` via the `update_segment_poa` skill.

## 8. Agent invocation (polling, no queue)

Lives in `workers/agent.py`. Each tick (~15s):

```python
while True:
    sleep(15)
    for mission in active_missions():
        events_since = SELECT events from pings/findings/dispatches/broadcasts
                       WHERE ts > mission.last_agent_invocation_ts

        should_invoke = (
            mission.force_agent_invoke
            or any(events_since matches a trigger row in the table below)
            or (now() - mission.last_agent_invocation_ts) > 60
        )

        if should_invoke:
            invoke(mission, trigger=summarize(events_since))
            UPDATE missions SET last_agent_invocation_ts = now(),
                                force_agent_invoke = 0
                            WHERE id = mission.id
```

Trigger table — events of these kinds in `events_since` cause an invocation. (Same semantics as the original queue gate, just diff-derived instead of enqueued.)

| # | Trigger              | Detected by                                                     |
| - | -------------------- | --------------------------------------------------------------- |
| 1 | `mission_start`      | mission row inserted, status → 'active' (force_agent_invoke=1)  |
| 2 | `subject_found`      | finding with `kind='subject_found'` (sets force_agent_invoke=1) |
| 3 | `finding_logged`     | any other finding except `kind='other'` w/ `confidence < 0.3`   |
| 4 | `segment_swept`      | spatial worker marks a segment swept                            |
| 5 | `divergence`         | searcher has ≥ 5 consecutive pings ≥ 100 m outside assigned segment |
| 6 | `no_comms`           | searcher's most recent ping > 10 min old (one-shot per outage)  |
| 7 | `dispatch_complete`  | dispatch marked `completed` by a searcher                       |
| 8 | `heartbeat`          | > 60s since last invocation, anything else happening            |
| 9 | `commander_override` | manual `POST /admin/agent/invoke` (sets force_agent_invoke=1)   |

Coalescing is implicit: a single tick reacts to all events since the last invocation in one openclaw call. No race between multiple workers pulling the same queue row.

## 9. Agent skills (tool interface)

The agent never gets raw SQL and never sees hexes. Skills are Python functions in `agent/skills/{read,write}.py`, exposed as openclaw tools with typed signatures. Every write skill writes an `agent_journal` entry; `reasoning` is a required arg on every write.

### Read skills

| Name                                              | Returns                                                                |
| ------------------------------------------------- | ---------------------------------------------------------------------- |
| `get_mission_brief()`                             | Markdown brief (see §10). Primary input.                               |
| `get_segment(id_or_name)`                         | Geometry summary, POA, POD, terrain stats, assigned searcher, active hazards intersecting |
| `get_searcher(id_or_callsign)`                    | Status, current dispatch, last-30min track summary                     |
| `get_findings(since_ts?, kind?)`                  | List of findings filtered                                              |
| `get_terrain_summary(segment_id)`                 | Slope distribution, dominant cover, trail density (aggregated from hexes) |
| `get_uncovered_areas(min_poa?)`                   | Ranked list of segments where (POA − POA·POD) is highest               |
| `query_route(from_lat, from_lon, to_lat, to_lon)` | Snap-to-nearest-trail waypoints via `ST_ClosestPoint` on osm_features  |

### Write skills (the agent's action surface)

| Name                                                                                                | Effect                                                                                                                  |
| --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `dispatch_searcher(user_id, segment_id, sweep_type, entry_lat, entry_lon, instruction, reasoning)`  | Inserts `dispatches` row; updates user.status → `dispatched`; segment.status → `assigned`, assigned_user_id; emits broadcast to that user |
| `reassign_searcher(user_id, new_segment_id, sweep_type, entry_lat, entry_lon, instruction, reasoning)` | Marks current dispatch `superseded`; creates new dispatch in same transaction                                        |
| `recall_searcher(user_id, return_lat, return_lon, instruction, reasoning)`                          | Creates dispatch with segment_id NULL; user.status → `returning`                                                        |
| `update_segment_poa(segment_id, new_poa, reasoning)`                                                | Updates poa column; logs reason                                                                                         |
| `flag_hazard(geom_geojson, kind, severity, description, reasoning)`                                 | Inserts hazard; rasterizes affected hex_cells.flag_danger; emits warning broadcast to any searcher whose current segment intersects |
| `broadcast(scope, kind, message, reasoning)`                                                        | Inserts broadcast row                                                                                                   |
| `update_mission_status(new_status, reasoning)`                                                      | Updates mission row (e.g. → `subject_found`, → `suspended`)                                                             |

**On hazard segment-level reasoning**: `get_segment(id)` returns active hazards intersecting that segment via `ST_Intersects(segments.geom, hazards.geom)` at read time. No hex involvement in the agent's view. Hex flag bookkeeping is a side-effect of `flag_hazard` for the renderer.

## 10. Mission Brief (input to agent)

Deterministic markdown, ~600 token target, regenerated by spatial_worker after each tick.

```markdown
# Mission Brief — {mission.name} — {now_local}

## Mission Status

- Subject: {subject_description}
- PLS: {pls_lat}, {pls_lon} @ {pls_ts_local} ({minutes_since_pls} min ago)
- Status: {mission.status}
- Active searchers: {n_dispatched}/{n_total}
- Cumulative POS: {sum_pos:.2f}

## Coverage Summary

- Total area: {total_km2} km²
- Segments swept: {n_swept}/{n_total}
- Highest remaining POA segments:
  - {seg.name} (POA={p:.2f}, POD={d:.2f}, {terrain_summary}, {trail_status})
  - ...

## Searchers

- {callsign} (status={status}, [on {seg}, sweep={type}, {minutes} min in, POD={current}/target {target}])
- ...

## Recent Findings (last 30 min)

- {hh:mm} by {reporter}, kind={kind}, conf={c:.1f}, at {lat,lon} ({segment}): "{description}"
- ...

## Active Hazards

- {kind} ({severity}, affecting segments {names}): {description}

## Recent Agent Actions (last 30 min)

- {hh:mm}: {action_summary} (reason: {reasoning_excerpt})

## Open Questions

- {auto-derived: searchers in no_comms, searchers approaching POD target, low-POA segments still assigned}
```

If a section is empty, omit it.

## 11. FastAPI endpoints

All endpoints except `/missions` and `/missions/join` require `X-Bearer-Token: <hex>` header. Plain HTTP through ngrok tunnel.

### Mission lifecycle (called by app launch flow)

| Method | Path                | Body / Effect                                                                                                                                                                    |
| ------ | ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| POST   | `/missions`         | `{name, subject_description, pls_lat, pls_lon, pls_ts, area_geojson, display_name, callsign?, hazards?}` → creates user + mission (creator becomes admin), precomputes terrain if needed, runs initial POA, seeds segments + hex_cells, rasterizes optional hazards, fires `mission_start`. Returns `{mission_id, join_code, bearer_token, user_id}`. |
| POST   | `/missions/join`    | `{join_code, display_name, callsign?, role?}` → creates user joined to that mission. Returns `{mission_id, bearer_token, user_id}`.                                              |

### Field tier (called by searcher app)

| Method | Path                              | Body / Effect                                                                                                                  |
| ------ | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| POST   | `/field/ping`                     | `{lat, lon, ts?, accuracy_m, speed_mps?, battery_pct?}` → 200. Append to `pings`. May trigger `divergence` / `no_comms_recovery`. |
| POST   | `/field/dispatch/{id}/ack`        | → 200. dispatch.status → `acked`.                                                                                              |
| POST   | `/field/dispatch/{id}/start`      | → 200. dispatch.status → `in_progress`; user.status → `on_segment`.                                                            |
| POST   | `/field/dispatch/{id}/complete`   | `{notes?}` → 200. dispatch.status → `completed`. Counts as `dispatch_complete` event.                                          |
| POST   | `/field/findings`                 | `{lat, lon, kind, description, confidence}` OR `{hex_id, kind, description, confidence}` → 201. Server resolves the other; sets containing hex's `flag_clue`. Fires `finding_logged` (or `subject_found`). |
| POST   | `/field/sos`                      | `{message?}` → 201. Inserts critical hazard + all-hands broadcast. Sets `force_agent_invoke=1`.                                |
| GET    | `/field/me`                       | → `{user, active_dispatch, segment_geojson, nearby_hazards, recent_broadcasts}`. Polled every 5 s.                             |
| GET    | `/field/me/route?segment_id=X`    | → list of `[lat, lon]` waypoints from current position to entry_point via snap-to-trail.                                       |
| GET    | `/field/announcements?since={ts}` | → broadcasts visible to this user since ts.                                                                                    |

### Mission tier (called by app map + dashboard)

| Method | Path                              | Returns                                                                                                                                         |
| ------ | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| GET    | `/mission/state.geojson`          | FeatureCollection: segments (color by POA/POD/status), hex_cells (only those with non-default flags, for color overlay), searchers (markers + recent tracks), findings, hazards, osm_features. Polled 10 s (app) / 3 s (dashboard). |
| GET    | `/mission/timeline?since={ts}`    | Chronological event feed: dispatches, findings, broadcasts, agent invocations, status changes.                                                  |
| GET    | `/mission/agent_journal?limit=20` | Recent agent reasoning entries.                                                                                                                 |
| GET    | `/mission/dashboard`              | Static HTML page (Leaflet).                                                                                                                     |

### Admin (mission creator only — checked against `missions.created_by_user_id`)

| Method | Path                         | Body / Effect                                                                                                                                                                    |
| ------ | ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| POST   | `/admin/agent/invoke`        | `{reason?}` → sets `missions.force_agent_invoke = 1`.                                                                                                                            |
| POST   | `/admin/mission/{id}/finish` | recalls all searchers, marks ended.                                                                                                                                              |

### Internal

`agent_worker` calls Python skill functions directly (in-process) — no HTTP round-trip. `replay_worker` writes via the real `/field/*` endpoints so the full ingestion path is exercised.

## 12. App screens

Stack: existing Expo + Swift project, stripped to 3 tabs + a header SOS button + a launch screen.

### Launch screen (before any tab)

Three choices: **Resume** (stored bearer_token), **Join mission** (enter join_code → optional display_name/callsign), **Create mission** (full mission setup form). After Join or Create, the bearer_token is persisted and the user lands on the Now tab.

Mission area creation in v1: tap to drop PLS, drag a radius slider for search area. Server generates a circular polygon. Freeform polygon drawing deferred.

### Now tab (default)

- **Current dispatch card**: big segment name, sweep type, instruction text, agent reasoning excerpt, ETA / time-on-segment, current POD vs target POD bar.
- **State machine buttons**: `Acknowledge` (pending → acked) → `Start sweep` (acked → in_progress) → `Mark complete` (in_progress → completed).
- **Latest broadcast banner** if any unread.
- **Other searchers strip**: row of fellow searchers with status dots.
- Pulled from `GET /field/me` every 5 s.

### Map tab

- Leaflet (or MapKit). Layers:
  - My position (blue dot, last 20 pings as breadcrumb)
  - Other searchers' positions (smaller markers, callsign label)
  - Assigned segment polygon (highlighted yellow border)
  - Other searchers' segments (faded)
  - Hex coverage overlay: visited hexes faint green, flag_danger red, flag_clue orange, flag_poi yellow, flag_impassable gray
  - Hazards (red overlay, tap → description from `hazards` row)
  - Findings (pins colored by kind, tap → details from `findings` row)
  - Trails / roads / water (from osm_features — drawn as lines, not as hexes)
  - Optional slope shading toggle (from hex_cells.slope_deg)
- "Get route to entry point" button → calls `/field/me/route?segment_id=X`, draws waypoints.
- Pulled from `GET /mission/state.geojson` every 10 s when tab open.

### Findings tab

- "Log a finding" form: pin position (defaults to current GPS, draggable), kind chip selector, description text, confidence slider, submit.
- Recent findings list (yours + others) with map preview.

### SOS button (persistent header)

- Confirms then `POST /field/sos` with current location → critical broadcast.

## 13. Mission Control dashboard (web, read-only)

Single HTML file under `dashboard/`, served by FastAPI at `/mission/dashboard`. Leaflet + vanilla JS, polls `GET /mission/state.geojson` every 3 s.

Layout:

- Main map fills viewport. Layer toggles in top-right: terrain shading, landcover, segments (with POA opacity), hex coverage, searchers + tracks, hazards, findings.
- Right rail (collapsible):
  - **Live agent journal** — latest reasoning at top, each entry shows trigger + tool calls + reasoning text
  - **Timeline** — same data as `/mission/timeline`, formatted
  - **Searcher status list** — callsign, current segment, current POD vs target
- Top bar: mission status, cumulative POS counter, elapsed time, big "Force agent invoke" + "End mission" buttons (POST to admin endpoints — require admin bearer).

## 14. Demo scenario

Recording at `recordings/demo_wilder_ranch.jsonl` — timed JSONL of events that the replay_worker injects via real `/field/*` endpoints.

**Beat sheet (5 min total):**

| Time   | Event                                                                                                       | Expected agent behavior                                                                                                                            |
| ------ | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| T+0:00 | Mission seeded. Subject: "12-year-old hiker in red jacket, last seen 90 min ago on Old Cove Landing trail." | Agent fires `mission_start`. Dispatches Alpha/Bravo/Charlie/Delta individually to top-4 POA segments, sweep types matched to segment size + terrain difficulty. |
| T+0:45 | Searchers ack dispatches, start moving.                                                                     | Dashboard shows tracks emerging from entry points; hex_visits accumulate; PODs begin rising.                                                       |
| T+1:30 | Alpha logs `footprint` finding, confidence 0.7, inside S-07.                                                | Agent fires `finding_logged`. POA bumps near S-07 → S-06 / S-08 priorities rise. Charlie reassigned from low-POA S-12 to S-08.                     |
| T+2:30 | Bravo's track goes ≥100 m outside assigned segment for 5 pings.                                             | Agent fires `divergence`. Sends route correction broadcast to Bravo with snap-to-trail waypoints back.                                             |
| T+3:15 | Delta hits POD ≥ target on S-09.                                                                            | `segment_swept` → agent dispatches Delta to next-highest unassigned segment S-11.                                                                  |
| T+4:00 | Charlie logs `subject_found`, confidence 1.0, in S-08.                                                      | Agent fires `subject_found`. Updates mission.status. Recalls Alpha → staging RTB. Dispatches Bravo → assist Charlie (extraction). All-hands broadcast. |
| T+4:45 | Closing dashboard view.                                                                                     | Cumulative POS, time-to-find, agent action count, full timeline.                                                                                   |

**Hybrid demo:** flip `MODE=hybrid` and one or two teammates carry phones for real; their real pings interleave with replay. Replay-only fallback if connectivity flakes.

## 15. File structure

```
geo-beacon/
├── README.md
├── CLAUDE.md
├── Hack-a-Claw.md                       # legacy v1 notes
├── docs/superpowers/specs/
│   └── 2026-05-15-sar-mission-control-design.md   # THIS FILE
├── migrations/
│   ├── 001_init.sql                     # users, missions, dispatches, broadcasts
│   ├── 002_spatial.sql                  # spatial init, missions.area_geom, pings, segments, findings, hazards
│   ├── 003_terrain.sql                  # hex_cells, hex_visits, osm_features
│   └── 004_agent.sql                    # agent_journal
├── api/
│   ├── main.py                          # FastAPI app + middleware
│   ├── db.py                            # SQLite + SpatiaLite connection helper
│   ├── auth.py                          # bearer-token middleware
│   ├── schemas.py                       # pydantic models
│   └── routes/
│       ├── missions.py                  # /missions, /missions/join
│       ├── field.py                     # /field/*
│       ├── mission.py                   # /mission/*
│       └── admin.py                     # /admin/*
├── workers/
│   ├── spatial.py                       # hex_visits / POD / divergence / brief regen
│   ├── agent.py                         # openclaw polling loop + tool exec
│   └── replay.py                        # demo sim
├── agent/
│   ├── brief.py                         # Mission Brief generator
│   └── skills/
│       ├── read.py
│       └── write.py
├── sar-app/                             # existing Expo, refactored to 3 tabs + launch screen
├── dashboard/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── scripts/
│   ├── fetch_terrain.py                 # DEM + landcover + OSM for bbox → hex_cells + osm_features
│   ├── seed_mission.py                  # creates mission + segments + initial POA + hex_cells population
│   ├── dgx.sh                           # ssh wrapper using ~/.dgx-pass + sshpass
│   ├── respawn-workers.sh               # kill tmux session, recreate with all workers
│   ├── record_demo.py                   # capture live session to jsonl
│   └── apply_migrations.py              # idempotent runner used by every worker startup
├── recordings/                          # tracked in git
│   └── demo_wilder_ranch.jsonl
├── requirements.txt
└── package.json                         # in sar-app/
```

## 16. Migration runner pattern

Every worker and the API call `scripts/apply_migrations.py` at startup. The script:

1. Ensures `schema_migrations` table exists.
2. Reads `migrations/*.sql` in lexical order.
3. For each file not yet in `schema_migrations`, executes inside a transaction, then inserts the filename.
4. Exits 0.

Deployment of a new migration = `git push` from laptop, `git pull` on DGX, restart workers. No SSH-side scripts to remember; the migrations apply automatically on next startup.

## 17. 20-hour implementation order

Parallelizable across teammates. Names below are role labels, not people.

**Phase 1 — foundations (hours 0–4, parallel)**

- **DB**: migrations 001–004, SpatiaLite loaded, apply_migrations.py, basic CRUD helpers in `api/db.py`.
- **API**: FastAPI scaffold, bearer auth, `/missions` + `/missions/join`, `/field` stub endpoints, `/mission/state.geojson` stub.
- **Map data**: `fetch_terrain.py` runnable for Wilder Ranch bbox; hex_cells + osm_features populated.
- **App**: existing Expo trimmed to launch screen + 3 tabs, `/field/me` polling working with mock data.

**Phase 2 — happy-path end-to-end (4–10h)**

- `seed_mission.py` with initial-POA heuristic and hex generation.
- Spatial worker: ping → hex_visits, POD recomputation, Mission Brief regen.
- Dispatch flow E2E: skill `dispatch_searcher` writes row → app sees + acks → start → complete.
- Mission Control dashboard renders state.geojson.

**Phase 3 — agent loop (10–14h)**

- `agent/brief.py` implementation against real schema.
- `agent_worker` polling loop: event diff, openclaw call, tool execution, journal write.
- Triggers 1, 2, 3, 4 wired.
- Write skills: `reassign_searcher`, `broadcast`, `flag_hazard` (statically rasterized at init), `update_segment_poa`.

**Phase 4 — demo polish (14–18h)**

- Replay worker + `demo_wilder_ranch.jsonl` authored.
- POA revision on findings (Gaussian bump implementation).
- Agent journal panel on app + dashboard.
- Route hint endpoint.
- Triggers 5–8 wired.

**Phase 5 — buffer + dry run (18–20h)**

- End-to-end demo rehearsal x 3, with hybrid + replay-only fallbacks tested.
- README + demo script + "what to say" cheat sheet.

## 18. Open architectural decisions

These are deliberately left for the team to call during implementation:

1. **POA revision sophistication** — Gaussian bump is simple; a particle filter would be richer but overkill for 20h.
2. **Hex size** — currently sized at ~30m corner-to-corner (~5k hexes per 2km × 2km). Larger hexes mean less spatial work but coarser coverage credit.
3. **Agent tick interval** — 15s is a guess; raise if agent feels expensive, lower if it feels slow to react.
4. **Spatial worker frequency** — 15s is a guess; tighten if dashboard feels stale during dry run.
5. **App-side map tile source** — bundled tiles via offline package vs. live OSM tiles requiring backhaul. Phones go through ngrok so backhaul exists, but cellular in real SAR doesn't — bundle for v2.
6. **Mission area type gate** — wilderness POA heuristic vs. urban (POA terms differ). Single mission for the demo; multi-environment routing is v2.

## 19. Out of scope, deferred to v2

- Multiple concurrent missions, full ICS hierarchy
- Teams as a first-class concept (the schema can re-add a `teams` table later if needed)
- Runtime hex-tap hazard / impassable flagging UI
- WhatsApp/Twilio integration (the comms-side parallel surface — likely v2 win)
- Photo + voice findings
- Map-matched graph routing on trail network
- K9, drone, aerial-asset modeling
- Battery / fatigue / rotation logic
- Real auth, rate limiting, retry semantics
- Offline-buffered ping submission from app
- Indoor / multi-floor search modeling
- Multi-tenant deployment (this is single-DGX, single-ngrok)

---

**Reviewer focus areas:** §5 data model (do the columns model real SAR?), §7 POD/POA math (right level of fidelity?), §11 endpoints (any missing?), §14 demo beat sheet (does this story sell the agent?).
