# SAR Mission Control — Design Spec

**Status:** Draft, awaiting review **Date:** 2026-05-15 **Hackathon budget:** 20
hours **Pivoted from:** personal life-pattern brief generator (see
`Hack-a-Claw.md`)

---

## 1. Mission

Build an **autonomous AI mission commander** for land search-and-rescue
operations.

The agent (openclaw on an NVIDIA DGX Spark) ingests:

- live searcher GPS tracks (via a thin mobile app),
- public terrain data (DEM-derived slope, ESA WorldCover landcover, OSM
  trails/roads/water),
- field-reported findings (clues, sightings, hazards, subject-found),

reasons about coverage, probability, and safety, and **issues structured
dispatches to individual searchers**. Each searcher's app polls FastAPI for
updates and renders their current orders, segment polygon, hazards, and the
status of other searchers in the field.

Mission Control **is** the AI. No human dispatcher in the loop. A commander web
dashboard exists for the demo audience to watch, not interact.

## 2. Scope (20-hour cut)

**In:**

- Single active mission at a time
- 4–8 individual searchers (no team layer; agent pairs by dispatching two searchers to the same segment when needed)
- Bounded search area (~2 km × 2 km) with pre-fetched terrain data
- App for each searcher: status, assignment, map, findings logging, SOS
- FastAPI on the DGX as the orchestration layer
- SQLite + SpatiaLite for state, persisted at `/home/asus/sqlite/mission.db`
- openclaw invoked event-driven via a gate, emits structured tool calls that
  write to DB
- Replay/sim worker for demo determinism (plus hybrid mode with real teammates)
- Read-only commander web dashboard (Leaflet + polling)

**Out (explicitly deferred):**

- Multiple concurrent missions
- ICS organizational hierarchy (we collapse to "agent is commander")
- Real authentication (single bearer token per user, plaintext fine for hack)
- Photo / audio findings (text + GPS + kind only)
- Offline buffering on the phone
- Map-matched routing (snap-to-nearest-trail only, no graph routing)
- WhatsApp / SMS integration (deferred to v2 — app-only this round)
- K9 / drone / aerial-asset special handling
- Battery / fatigue management
- Anything that doesn't directly enable the demo narrative

## 3. Personas

- **Searcher**: ground volunteer with a phone running the app. Receives
  assignment from agent, follows it, logs findings, reports complete.
- **Agent (openclaw)**: assumes the combined role of Incident Commander +
  Planning Chief + Operations Chief in real-SAR ICS terms. Decides dispatches,
  revises POA on new evidence, recalls searchers, flags hazards.
- **Observer (judge / demo viewer)**: watches Mission Control web dashboard
  during demo; doesn't interact.

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
                            │ HTTPS over hotspot LAN
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│  FASTAPI on DGX (orchestrator)                                     │
│   routes/field.py     — searcher writes + me-state reads           │
│   routes/mission.py   — geojson state, timeline, agent journal     │
│   routes/admin.py     — mission setup, force-invoke, finish        │
│   gate.py             — decides whether each write fires agent     │
└───────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│  SQLite + SpatiaLite at /home/asus/sqlite/mission.db (WAL mode)                │
│  Tables: missions, users, segments, pings, dispatches, findings,   │
│  hazards, broadcasts, agent_journal, coverage_cache,               │
│  terrain_cells, osm_features                                       │
└───────────────────────────┬────────────────────────────────────────┘
                            │
   ┌────────────────────────┴─────────────────────────────────┐
   │  Worker processes (tmux, sleep-loop pattern):            │
   │                                                          │
   │  • spatial_worker  (every 30s)                           │
   │      tracks → ST_MakeLine → ST_Buffer → ST_Union         │
   │      → ST_Intersection w/ segment → POD per segment      │
   │      → coverage_cache, divergence flags, brief rebuild   │
   │                                                          │
   │  • agent_worker    (event-driven via DB queue)           │
   │      pops invocation → loads Mission Brief →             │
   │      calls openclaw → executes tool calls →              │
   │      writes journal entry                                │
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

**Network assumption:** all laptops + DGX share a phone-hotspot SSID. SSH from
laptop → DGX for deploy. App talks to DGX over the same LAN by IP. No public
ingress. Deploy =
`ssh dgx 'cd geo-beacon && git pull && ./scripts/respawn-workers.sh'`.

## 5. Data model

All geometry columns are SpatiaLite-managed; create with `AddGeometryColumn` and
`CreateSpatialIndex`.

### `users`

Each searcher is a user. Callsigns (Alpha, Bravo, ...) live here directly; no
separate team layer.

```
id            INTEGER PRIMARY KEY
display_name  TEXT NOT NULL
callsign      TEXT UNIQUE          -- 'Alpha', 'Bravo', ...; null for observers
phone         TEXT                 -- for future Twilio integration
role          TEXT NOT NULL        -- 'searcher' | 'team_leader' | 'observer'
status        TEXT NOT NULL        -- 'standby' | 'dispatched' | 'on_segment' | 'returning' | 'no_comms' | 'off_duty'
bearer_token  TEXT UNIQUE NOT NULL -- random hex, single-mission scope
created_ts    INTEGER NOT NULL
```

### `missions`

```
id                   INTEGER PRIMARY KEY
name                 TEXT NOT NULL
status               TEXT NOT NULL  -- 'planning' | 'active' | 'subject_found' | 'suspended' | 'ended'
subject_description  TEXT NOT NULL  -- "12yo male hiker in red jacket, last seen 14:20"
pls_lat              REAL NOT NULL  -- Point Last Seen
pls_lon              REAL NOT NULL
pls_ts               INTEGER NOT NULL
area_geom            BLOB NOT NULL  -- POLYGON, bounding search area
started_ts           INTEGER NOT NULL
ended_ts             INTEGER
```

### `segments`

The search sectors. Polygon geometry; POA assigned at start, revised on
findings.

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
-- assignee derived from open dispatches; no FK column on segments
sweep_type        TEXT                   -- 'hasty' | 'efficient' | 'thorough'
target_pod        REAL                   -- 0.5 / 0.7 / 0.85 per sweep type
avg_slope_deg     REAL NOT NULL          -- precomputed from DEM
dominant_cover    TEXT NOT NULL          -- 'open' | 'mixed' | 'dense' | 'water' | 'rock'
trail_length_m    REAL NOT NULL DEFAULT 0
INDEX (mission_id, status)
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

The agent's primary write surface. One row = one order.

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

Reported by searchers via the app. Always fire the agent gate.

```
id                INTEGER PRIMARY KEY
mission_id        INTEGER NOT NULL REFERENCES missions(id)
reporter_user_id  INTEGER NOT NULL REFERENCES users(id)
ts                INTEGER NOT NULL
lat               REAL NOT NULL
lon               REAL NOT NULL
geom              BLOB NOT NULL          -- POINT
kind              TEXT NOT NULL          -- 'clue' | 'subject_found' | 'subject_sighting' | 'hazard'
                                         -- | 'footprint' | 'discarded_item' | 'other'
description       TEXT NOT NULL
confidence        REAL NOT NULL          -- 0.0–1.0, self-assessed
photo_url         TEXT                   -- deferred for hack
INDEX (mission_id, ts DESC)
SpatialIndex on geom
```

### `hazards`

Agent- or commander-flagged dangers; rendered on app + dashboard maps.

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

Agent → app messages, scoped to all-hands or a specific user.

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

One row per agent invocation, for transparency.

```
id              INTEGER PRIMARY KEY
mission_id      INTEGER NOT NULL REFERENCES missions(id)
ts              INTEGER NOT NULL
trigger         TEXT NOT NULL          -- gate reason
brief_md        TEXT NOT NULL          -- snapshot of Mission Brief input
tool_calls_json TEXT NOT NULL          -- array of {tool, args, result}
reasoning       TEXT                   -- agent's narration if returned
duration_ms     INTEGER NOT NULL
INDEX (mission_id, ts DESC)
```

### `coverage_cache`

Recomputed by spatial worker every 30 s.

```
segment_id        INTEGER PRIMARY KEY REFERENCES segments(id)
covered_area_m2   REAL NOT NULL
covered_geom      BLOB                 -- union of buffered tracks ∩ segment
pod_current       REAL NOT NULL
last_computed_ts  INTEGER NOT NULL
```

### `terrain_cells` (precomputed per mission area)

### Shreyan note: will "segmnets" need to have a higher resolution than terrain cells? Otherwise I believe we could

### merge the functionality of "segments" and this using a "discovered" column. Or maybe each cell is split into 10 sub-cells, each with an exploration status.

100 m × 100 m grid covering the mission area.

```
id              INTEGER PRIMARY KEY
mission_id      INTEGER NOT NULL REFERENCES missions(id)
geom            BLOB NOT NULL          -- POLYGON
center_elev_m   REAL NOT NULL
avg_slope_deg   REAL NOT NULL
dominant_cover  TEXT NOT NULL          -- 'open' | 'mixed' | 'dense' | 'water' | 'rock'
SpatialIndex on geom
```

### `osm_features` (precomputed)

Trails, roads, water bodies — for route hints and app map base layer.

```
id              INTEGER PRIMARY KEY
mission_id      INTEGER NOT NULL REFERENCES missions(id)
kind            TEXT NOT NULL          -- 'trail' | 'road' | 'water' | 'building'
name            TEXT
geom            BLOB NOT NULL          -- LINESTRING or POLYGON
SpatialIndex on geom
```

### `agent_invocation_queue`

Trivial FIFO consumed by agent_worker.

```
id          INTEGER PRIMARY KEY
mission_id  INTEGER NOT NULL
trigger     TEXT NOT NULL
context     TEXT                       -- JSON, e.g. {finding_id: 42}
created_ts  INTEGER NOT NULL
claimed_ts  INTEGER
```

## 6. Public map data integration

Pre-fetched once per mission area by `scripts/fetch_terrain.py`, given a
bounding box:

1. **USGS NED 1/3 arc-second DEM** → GeoTIFF for area. Use `rasterio` + `numpy`
   to derive slope raster: `slope = arctan(magnitude(gradient(elev)))`. Resample
   to 100 m grid → `terrain_cells.avg_slope_deg`, `.center_elev_m`.
2. **ESA WorldCover 2021** (10 m) → classify each terrain cell. Map their
   classes to our 5 buckets: `open` (grassland, cropland, bare), `mixed`
   (shrubland, sparse tree), `dense` (closed forest), `water`, `rock`
   (snow/ice/bare rock).
3. **OSM via Overpass API**: pull `highway in (path, footway, track)` as
   `trail`; `highway in (primary, secondary, tertiary, residential, service)` as
   `road`; `natural=water`, `waterway=stream|river` as `water`; `building=*` as
   `building`. Insert as `osm_features`.

For demo: pre-fetch **Wilder Ranch State Park** (Santa Cruz, CA) — real
elevation variation, real trails, accessible to demo presenter. Bbox roughly
36.95°N–37.00°N, -122.10°W–-122.05°W.

Outputs of fetch script: populated `terrain_cells` + `osm_features` for the
mission area; static tile cache (optional) under `/data/terrain/<mission>/` for
the dashboard base layer.

## 7. POD / POA math

We use standard Koopman-style detection theory, adjusted for terrain.

### Initial POA assignment

At mission seed time, given PLS and mission area:

1. Subdivide area into ~100 m × 100 m square segments(these become `segments`
   rows).
2. For each segment, compute raw weight:
   ```
   d = distance(segment_center, pls)
   dist_term      = exp(-d² / (2 · σ²))    where σ = 750 m
   trail_term     = 1.5 if segment overlaps trail else 1.0
   downhill_term  = 1.0 + 0.002 · max(0, pls_elev - segment_center_elev)
   cover_term     = 0.7 if dominant_cover='dense' else 1.0
   raw_w = dist_term · trail_term · downhill_term · cover_term
   ```
3. Normalize: `poa[i] = raw_w[i] / Σ raw_w`

(All three boosts are documented lost-person behavior heuristics from
ISRID-style literature, simplified.)

### Effective sweep width per segment

```
base_W = { hasty: 100, efficient: 50, thorough: 25 }     # meters
veg_factor = { open: 1.0, mixed: 0.6, dense: 0.3, water: 0.0, rock: 0.8 }
slope_factor = max(0.3, 1 - avg_slope_deg / 45)

W_eff = base_W[sweep_type] · veg_factor[dominant_cover] · slope_factor
```

### POD per segment

```
L = total length of assigned searcher(s) track LINESTRING inside the segment polygon
A = segment area in m²
POD = 1 - exp( -W_eff · L / A )
```

### POS

```
POS = POA · POD          # per segment
mission_POS = Σ POS      # cumulative; primary success metric for demo
```

### Sweep-complete threshold

```
target_pod = { hasty: 0.5, efficient: 0.7, thorough: 0.85 }
```

When `pod_current ≥ target_pod` for an in-progress segment, the spatial worker
marks it `swept`, frees the searcher(s), and fires the `segment_swept` gate event.

### POA revision on findings

When a finding is logged at `(f_lat, f_lon)` with confidence `c`:

1. Gaussian bump centered on finding, σ = 300 m, magnitude `0.4 · c` of total
   prior POA.
2. Add bump to each segment's POA in proportion to overlap area.
3. Subtract proportionally from all segments currently marked `swept` (we
   believe subject isn't there).
4. Renormalize so Σ POA = 1.

Always logged in `agent_journal` via `update_segment_poa` skill.

## 8. Agent invocation gate

Lives in `api/gate.py`. Called from any handler/worker that writes meaningful
state. First match wins; events with no match are dropped with
`trigger: 'no_trigger'`. Gate enqueues an entry into `agent_invocation_queue`
rather than calling the agent directly — keeps the API endpoint fast and gives
natural backpressure.

Rate limit: cap at 1 invocation per 20 s per mission; pending entries coalesce
into a single dequeue with combined trigger list.

| # | Trigger                  | Fires when                                                          |
| - | ------------------------ | ------------------------------------------------------------------- |
| 1 | `mission_start`          | mission row inserted, status → 'active'                             |
| 2 | `subject_found`          | finding with `kind='subject_found'` (highest priority)              |
| 3 | `finding_logged`         | any other finding except `kind='other'` w/ `confidence < 0.3`       |
| 4 | `segment_swept`          | coverage_cache update pushes pod_current ≥ target_pod               |
| 5 | `divergence`             | searcher has ≥ 5 consecutive pings ≥ 100 m outside assigned segment |
| 6 | `no_comms`               | searcher's most recent ping > 10 min old (one-shot per outage)      |
| 7 | `searcher_complete_ack`  | dispatch marked `completed` by a searcher                           |
| 8 | `heartbeat`              | > 15 min since last invocation, anything else happening             |
| 9 | `commander_override`     | manual `POST /admin/agent/invoke`                                   |

## 9. Agent skills (tool interface)

The agent never gets raw SQL. Skills are Python functions in
`agent/skills/{read,write}.py`, exposed as openclaw tools with typed signatures.
Every write skill also writes an `agent_journal` entry; `reasoning` is a
required arg on every write.

### Read skills

| Name                                              | Returns                                                               |
| ------------------------------------------------- | --------------------------------------------------------------------- |
| `get_mission_brief()`                             | Markdown brief (see §10). Primary input.                              |
| `get_segment(id_or_name)`                         | Geometry summary, POA, POD, terrain stats, current/recent assignments |
| `get_searcher(id_or_callsign)`                    | Status, current dispatch, last-30min track summary                    |
| `get_findings(since_ts?, kind?)`                  | List of findings filtered                                             |
| `get_terrain_summary(segment_id)`                 | Slope distribution, dominant cover, trail density                     |
| `get_uncovered_areas(min_poa?)`                   | Ranked list of segments where (POA − POA·POD) is highest              |
| `query_route(from_lat, from_lon, to_lat, to_lon)` | Snap-to-nearest-trail waypoints (no graph routing)                    |

### Write skills (the agent's action surface)

| Name                                                                                                       | Effect                                                                                                                          |
| ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `dispatch_searcher(user_id, segment_id, sweep_type, entry_lat, entry_lon, instruction, reasoning)`         | Inserts `dispatches` row; updates user.status → `dispatched`; segment.status → `assigned`; emits broadcast to that searcher     |
| `reassign_searcher(user_id, new_segment_id, sweep_type, entry_lat, entry_lon, instruction, reasoning)`     | Marks current dispatch `superseded`; creates new dispatch in same transaction                                                   |
| `recall_searcher(user_id, return_lat, return_lon, instruction, reasoning)`                                 | Creates dispatch with segment_id NULL; user.status → `returning`                                                                |
| `update_segment_poa(segment_id, new_poa, reasoning)`                                                       | Updates poa column; logs reason                                                                                                 |
| `flag_hazard(geom_geojson, kind, severity, description, reasoning)`                                        | Inserts hazard; emits warning broadcast to any searcher whose current segment intersects                                        |
| `broadcast(scope, kind, message, reasoning)`                                                               | Inserts broadcast row                                                                                                           |
| `update_mission_status(new_status, reasoning)`                                                             | Updates mission row (e.g. → `subject_found`, → `suspended`)                                                                     |

**Pairing two searchers on one segment:** the agent calls
`dispatch_searcher` twice for the same `segment_id`. There's no separate
"team" abstraction enforcing pairing; coordination is purely at the agent's
discretion.

## 10. Mission Brief (input to agent)

Deterministic markdown, ~600 token target, regenerated by spatial_worker after
each event.

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

- {callsign} (status={status}, [on {seg}, sweep={type}, {minutes} min in,
  POD={current}/target {target}])
- ...

## Recent Findings (last 30 min)

- {hh:mm} by {reporter}, kind={kind}, conf={c:.1f}, at {lat,lon} ({segment}):
  "{description}"
- ...

## Active Hazards

- {kind} ({severity}): {description}

## Recent Agent Actions (last 30 min)

- {hh:mm}: {action_summary} (reason: {reasoning_excerpt})

## Open Questions

- {auto-derived: searchers in no_comms, searchers approaching POD target,
  low-POA segments still assigned}
```

If a section is empty, omit it.

## 11. FastAPI endpoints

All endpoints require `X-Bearer-Token: <hex>` header. Plain HTTP over hotspot
LAN.

### Field tier (called by searcher app)

| Method | Path                              | Body / Effect                                                                                                                  |
| ------ | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| POST   | `/field/ping`                     | `{lat, lon, ts?, accuracy_m, speed_mps?, battery_pct?}` → 200. Append to `pings`. May fire `divergence` / `no_comms_recovery`. |
| POST   | `/field/dispatch/{id}/ack`        | → 200. dispatch.status → `acked`.                                                                                              |
| POST   | `/field/dispatch/{id}/start`      | → 200. dispatch.status → `in_progress`; user.status → `on_segment`.                                                            |
| POST   | `/field/dispatch/{id}/complete`   | `{notes?}` → 200. dispatch.status → `completed`. Fires `searcher_complete_ack`.                                                |
| POST   | `/field/findings`                 | `{lat, lon, kind, description, confidence}` → 201. Fires `finding_logged` (or `subject_found`).                                |
| POST   | `/field/sos`                      | `{message?}` → 201. Inserts critical hazard + all-hands broadcast. Fires `commander_override`.                                 |
| GET    | `/field/me`                       | → `{user, active_dispatch, segment_geojson, nearby_hazards, recent_broadcasts}`. Polled every 5 s.                             |
| GET    | `/field/me/route?segment_id=X`    | → list of `[lat, lon]` waypoints from current position to entry_point via snap-to-trail.                                       |
| GET    | `/field/announcements?since={ts}` | → broadcasts visible to this user since ts.                                                                                    |

### Mission tier (called by app map + dashboard)

| Method | Path                              | Returns                                                                                                                                         |
| ------ | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| GET    | `/mission/state.geojson`          | FeatureCollection: segments (color by POA/POD/status), searchers (markers + recent tracks), findings, hazards. Polled 10 s (app) / 3 s (dashboard). |
| GET    | `/mission/timeline?since={ts}`    | Chronological event feed: dispatches, findings, broadcasts, gate fires, status changes.                                                         |
| GET    | `/mission/agent_journal?limit=20` | Recent agent reasoning entries.                                                                                                                 |
| GET    | `/mission/dashboard`              | Static HTML page (Leaflet).                                                                                                                     |

### Admin / commander

| Method | Path                         | Body / Effect                                                                                                                                                                    |
| ------ | ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| POST   | `/admin/mission`             | `{name, subject_description, pls_lat, pls_lon, pls_ts, area_geojson}` → creates mission, precomputes terrain if needed, runs initial POA, seeds segments, fires `mission_start`. |
| POST   | `/admin/users`               | `{display_name, callsign, role}` → creates searcher; returns bearer token.                                                                                                       |
| POST   | `/admin/agent/invoke`        | `{reason?}` → enqueues a `commander_override` trigger.                                                                                                                           |
| POST   | `/admin/mission/{id}/finish` | recalls all searchers, marks ended.                                                                                                                                              |

### Internal

`agent_worker` calls Python skill functions directly (in-process) — no HTTP
round-trip. `replay_worker` writes via the real `/field/*` endpoints so the full
ingestion path is exercised.

## 12. App screens

Stack: existing Expo + Swift project, stripped to 4 tabs + an SOS header button.
Polling fetches; no websockets.

### Now tab (default)

- **Current dispatch card**: big segment name, sweep type, instruction text,
  agent reasoning excerpt, ETA / time-on-segment, current POD vs target POD bar.
- **State machine buttons**: `Acknowledge` (pending → acked) → `Start sweep`
  (acked → in_progress) → `Mark complete` (in_progress → completed).
- **Latest broadcast banner** if any unread.
- **Other searchers strip**: row of other active searchers with status dots.
- Pulled from `GET /field/me` every 5 s.

### Map tab

- Leaflet (or MapKit). Layers:
  - My position (blue dot, last 20 pings as breadcrumb)
  - Other searchers' positions (smaller markers, callsign label)
  - Assigned segment polygon (highlighted yellow border)
  - Other searchers' assigned segments (faded)
  - Hazards (red overlay, tap → description)
  - Findings (pins colored by kind, tap → details)
  - Trails / roads / water (from osm_features)
  - Optional slope shading toggle (from terrain_cells)
- "Get route to entry point" button → calls `/field/me/route?segment_id=X`,
  draws waypoints.
- Pulled from `GET /mission/state.geojson` every 10 s when tab open.

### Findings tab

- "Log a finding" form: pin position (defaults to current GPS, draggable), kind
  chip selector, description text, confidence slider, submit.
- Recent findings list (yours + others) with map preview.

### Mission tab

- All searchers + statuses
- Mission timeline (latest at top, scrollable)
- Agent journal entries (collapsible — tap to expand reasoning)
- Mission status banner ("ACTIVE", "SUBJECT FOUND", etc.)

### SOS button (persistent header)

- Confirms then `POST /field/sos` with current location → critical broadcast.

## 13. Mission Control dashboard (web, read-only)

Single HTML file under `dashboard/`, served by FastAPI at `/mission/dashboard`.
Leaflet + vanilla JS, polls `GET /mission/state.geojson` every 3 s.

Layout:

- Main map fills viewport. Layer toggles in top-right: terrain shading,
  landcover, segments (with POA opacity), searchers + tracks, hazards, findings.
- Right rail (collapsible):
  - **Live agent journal** — latest reasoning at top, each entry shows trigger +
    tool calls + reasoning text
  - **Timeline** — same data as `/mission/timeline`, formatted
  - **Searcher status list** — callsign, current segment, current POD vs target
- Top bar: mission status, cumulative POS counter, elapsed time, big "Force
  agent invoke" + "End mission" buttons (POST to admin endpoints).

## 14. Demo scenario

Recording at `/data/recordings/demo_wilder_ranch.jsonl` — timed JSONL of events
that the replay_worker injects via real `/field/*` endpoints.

**Beat sheet (5 min total):**

| Time   | Event                                                                                                       | Expected agent behavior                                                                                                                            |
| ------ | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| T+0:00 | Mission seeded. Subject: "12-year-old hiker in red jacket, last seen 90 min ago on Old Cove Landing trail." | Agent fires `mission_start`. Dispatches searchers Alpha/Bravo/Charlie/Delta to top-4 POA segments, sweep types matched to segment size + terrain difficulty. |
| T+0:45 | Searchers ack dispatches, start moving.                                                                     | Dashboard shows tracks emerging from entry points; PODs begin rising.                                                                                        |
| T+1:30 | Alpha logs `footprint` finding, confidence 0.7, inside S-07.                                                | Agent fires `finding_logged`. POA bumps near S-07 → S-06 / S-08 priorities rise. Charlie reassigned from low-POA S-12 to S-08.                               |
| T+2:30 | Bravo's track goes ≥100 m outside assigned segment for 5 pings.                                             | Agent fires `divergence`. Sends route correction broadcast to Bravo with snap-to-trail waypoints back.                                                       |
| T+3:15 | Delta hits POD ≥ target on S-09.                                                                            | `segment_swept` → agent dispatches Delta to next-highest unassigned segment S-11.                                                                            |
| T+4:00 | Charlie logs `subject_found`, confidence 1.0, in S-08.                                                      | Agent fires `subject_found`. Updates mission.status. Dispatches Alpha → staging RTB. Bravo → assist Charlie (extraction). All-hands broadcast.               |
| T+4:45 | Closing dashboard view.                                                                                     | Cumulative POS, time-to-find, agent action count, full timeline.                                                                                   |

**Hybrid demo:** flip `MODE=hybrid` and one or two teammates carry phones for
real; their real pings interleave with replay. Replay-only fallback if
connectivity flakes.

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
│   ├── 002_spatial.sql                  # enable SpatiaLite, add geometry columns + spatial indices
│   ├── 003_terrain.sql                  # terrain_cells, osm_features
│   └── 004_queue.sql                    # agent_invocation_queue, agent_journal, coverage_cache
├── api/
│   ├── main.py                          # FastAPI app + middleware
│   ├── db.py                            # SQLite + SpatiaLite connection helper
│   ├── auth.py                          # bearer-token middleware
│   ├── schemas.py                       # pydantic models
│   ├── gate.py                          # invocation gate
│   └── routes/
│       ├── field.py
│       ├── mission.py
│       └── admin.py
├── workers/
│   ├── spatial.py                       # coverage / POD / divergence / brief regen
│   ├── agent.py                         # openclaw invocation + tool exec
│   └── replay.py                        # demo sim
├── agent/
│   ├── brief.py                         # Mission Brief generator
│   └── skills/
│       ├── read.py
│       └── write.py
├── app/                                 # existing Expo/Swift, refactored to 4 tabs
├── dashboard/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── scripts/
│   ├── fetch_terrain.py                 # DEM + landcover + OSM for bbox
│   ├── seed_mission.py                  # creates mission + segments + initial POA
│   ├── setup_dgx.sh                     # one-time DGX setup (deps, mkdir, etc)
│   ├── deploy.sh                        # ssh dgx 'cd geo-beacon && git pull && ./scripts/respawn-workers.sh'
│   ├── respawn-workers.sh               # kill tmux session, recreate with all workers
│   ├── record_demo.py                   # capture live session to jsonl
│   └── apply_migrations.py              # idempotent runner used by every worker startup
├── data/                                # .gitignored
│   ├── mission.db
│   ├── terrain/
│   └── recordings/
│       └── demo_wilder_ranch.jsonl
├── pyproject.toml
└── package.json                         # in app/
```

## 16. Migration runner pattern

Every worker and the API call `scripts/apply_migrations.py` at startup. The
script:

1. Ensures `schema_migrations` table exists.
2. Reads `migrations/*.sql` in lexical order.
3. For each file not yet in `schema_migrations`, executes inside a transaction,
   then inserts the filename.
4. Exits 0.

Deployment of a new migration = `git push` from laptop, `git pull` on DGX,
restart workers. No SSH-side scripts to remember; the migrations apply
automatically on next startup.

## 17. 20-hour implementation order

Parallelizable across teammates. Names below are role labels, not people.

**Phase 1 — foundations (hours 0–4, parallel)**

- **DB**: migrations 001–004, SpatiaLite loaded, apply_migrations.py, basic CRUD
  helpers.
- **API**: FastAPI scaffold, bearer auth, /field stub endpoints,
  /mission/state.geojson stub.
- **Map data**: fetch_terrain.py runnable for Wilder Ranch bbox, terrain_cells
  populated.
- **App**: existing Expo trimmed to 4 tabs, /field/me polling working with mock
  data.

**Phase 2 — happy-path end-to-end (4–10h)**

- seed_mission.py with initial-POA heuristic.
- Spatial worker: track aggregation, coverage_cache, POD math, Mission Brief
  regen.
- Dispatch flow E2E: skill `dispatch_searcher` writes row → app sees + acks → start
  → complete.
- Mission Control dashboard renders state.geojson.

**Phase 3 — agent loop (10–14h)**

- agent/brief.py implementation against real schema.
- agent_worker scaffold: queue drain, openclaw call, tool execution.
- Gate triggers 1, 2, 3, 4 wired.
- Write skills: `reassign_searcher`, `broadcast`, `flag_hazard`,
  `update_segment_poa`.

**Phase 4 — demo polish (14–18h)**

- Replay worker + demo_wilder_ranch.jsonl authored.
- POA revision on findings (Gaussian bump implementation).
- Hazard flagging UX on app + dashboard.
- Agent journal panel on app + dashboard.
- Route hint endpoint.
- Gate triggers 5–8 wired.

**Phase 5 — buffer + dry run (18–20h)**

- End-to-end demo rehearsal x 3, with hybrid + replay-only fallbacks tested.
- README + demo script + "what to say" cheat sheet.

## 18. Open architectural decisions

These are deliberately left for the team to call during implementation:

1. **POA revision sophistication** — Gaussian bump is simple; a particle filter
   would be richer but overkill for 20h.
2. **Sweep width values** — borrowed from real-SAR ranges but uncalibrated to
   our demo terrain. Tune during dry runs.
3. **Agent rate limit** — 20 s per mission is a guess; raise if agent feels
   slow.
4. **Spatial worker frequency** — 30 s is a guess; tighten if dashboard feels
   stale during dry run.
5. **App-side map tile source** — bundled tiles via offline package vs. live OSM
   tiles requiring backhaul. For hotspot scenarios, prefer bundled.

## 19. Out of scope, deferred to v2

- Multiple concurrent missions, full ICS hierarchy
- WhatsApp/Twilio integration (the comms-side parallel surface — likely v2 win)
- Photo + voice findings
- Map-matched graph routing on trail network
- K9, drone, aerial-asset modeling
- Battery / fatigue / rotation logic
- Real auth, rate limiting, retry semantics
- Offline-buffered ping submission from app
- Multi-tenant deployment (this is single-DGX, single-LAN)

---

**Reviewer focus areas:** §5 data model (do the columns model real SAR?), §7
POD/POA math (right level of fidelity?), §11 endpoints (any missing?), §14 demo
beat sheet (does this story sell the agent?).
