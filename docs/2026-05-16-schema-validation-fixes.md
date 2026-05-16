# 2026-05-16 — Schema + validation fixes

Fixes for the issues surfaced during the recent review. Each item is tied to its issue code so you can cross-reference your notes. Branch: `schema-validation-fixes`. PR: open.

## What landed

### Migrations

No new migration file. Since the dev DB is throwaway, the canonical schema in `migrations/001_init.sql` and `migrations/002_spatial.sql` was edited directly:

- **`migrations/001_init.sql`** — `users` definition updated:
  - Added `current_mission_id INTEGER REFERENCES missions(id)`.
  - Replaced global `UNIQUE(callsign)` with `UNIQUE(current_mission_id, callsign)`.
  - Added `idx_users_mission` index on `current_mission_id`.
- **`migrations/002_spatial.sql`** — `findings.description` is now nullable.

**Upgrade path on existing dev DBs**: delete `~/sqlite/mission.db` (or your local equivalent) and let the migration runner re-apply from scratch on next worker / API startup. Since `schema_migrations` tracks by filename, an already-applied 001 won't re-run automatically with the new content — wipe is required.

### Code changes by issue code

| Code | Severity | Where | What changed |
|---|---|---|---|
| **F-1** | critical | `api/routes/field.py` | `POST /field/findings` was 500ing on every call. Fixed column names (`reporter_user_id`, `ts`), now writes the NOT NULL `geom POINT` via `SetSRID(MakePoint(lon, lat), 4326)`. Also added the hex-marking branch: `kind='hazard'` now also inserts a hazards row (polygon = containing hex's geom) and rasterizes to `flag_danger`. Failed hex lookup now returns 422 instead of swallowing and 500ing on NULL hex_id. |
| **F-2** | medium | `migrations/002_spatial.sql` + `api/schemas.py` | `findings.description` is now nullable both in the DB and on the wire. `FindingRequest.description: Optional[str]` was already optional; the schema matches it. |
| **F-3** | medium | `api/schemas.py` | `FindingRequest.kind` is now `Literal[...]` matching the DB CHECK exactly (`clue`, `subject_found`, `subject_sighting`, `hazard`, `footprint`, `discarded_item`, `note`, `other`). Bogus kinds rejected with 422 by Pydantic before they hit the DB. |
| **H-2** | medium | `api/schemas.py` | `HazardInput.poly_geojson` now rejects `MultiPolygon`. `hazards.geom` is registered as POLYGON; SpatiaLite would have 500'd at INSERT. |
| **MI-1** | medium | `api/schemas.py` | `CreateMissionRequest.area_geojson` rejects `MultiPolygon` for the same reason on `missions.area_geom`. |
| **U-1** | low (workaround existed) | `migrations/001_init.sql` + `api/db/users.py`, `api/db/missions.py`, `api/routes/missions.py` | `users.callsign` is now per-mission UNIQUE via `(current_mission_id, callsign)`. The `_disambiguate_callsign` workaround (Alpha → Alpha-2) is gone. Same callsign in the same mission now returns a clean 409 from `/missions/join`. `active_mission_id_for_user` reads `users.current_mission_id` directly (with the previous created-by/pings inference as a defensive fallback). `POST /missions` sets the creator's `current_mission_id` automatically; `POST /missions/join` sets the joiner's. |
| **HC-1** | cosmetic | `scripts/fetch_terrain.py` | `hex_cells` actually stores hexagons now. Switched to flat-top hexes on an odd-r offset grid. Each cell has 6 unique vertices. ~5000 cells per 2×2km mission area is unchanged. `scripts/seed_hex_cells.py` didn't need changes — the segment bucket lookup keys on hex centroid lat/lon, not on hex topology. |

### Pydantic audit notes

While tightening, I went through every CHECK / NOT NULL constraint in migrations 001–004. The following DB enums **don't** surface in request models because they're server-set or internal state-machine values — no Pydantic change needed, but worth knowing if any of these ever become wire-controlled:

- `users.status` (`standby` / `dispatched` / `on_segment` / `returning` / `no_comms` / `off_duty`) — server transitions.
- `missions.status` (`planning` / `active` / `subject_found` / `suspended` / `ended`) — server lifecycle.
- `dispatches.status`, `dispatches.sweep_type` — agent-set.
- `broadcasts.kind` (`info` / `warning` / `recall` / `finding_alert` / `route_correction`) — agent/server emits.
- `pings.source` (`phone` / `replay` / `manual`) — set by the ingest path; intentionally off the wire.
- `segments.status`, `segments.dominant_cover` — seeded server-side.

Module-level `Literal` aliases (`FindingKind`, `HazardKind`, `HazardSeverity`, `UserRole`) were added so future request models can reuse them. The old runtime sets (`VALID_HAZARD_KINDS` etc.) are gone.

## Issues from review that were not addressed

| Code | Reason |
|---|---|
| **AP-1, AP-2, AP-3** | Based on `docs/app-plan.md`, which doesn't exist in the repo (we have `docs/app-build.md`, which references a completely different / older API surface). If your teammate wants the URL shapes `/missions/active` or `/missions/{id}/join`, that's a 10-line alias whenever needed — flag and we'll add. |
| **HV-1** (`hex_visits` lacks `UNIQUE(hex_id, user_id)`) | Real concern, but not on the immediate fix list. Spec §327 needs the constraint for `INSERT OR IGNORE` semantics. Will be addressed when the spatial worker lands (Phase 3). Easy follow-up migration. |
| **`api/db.py` duplicate of `api/db/__init__.py`** | False alarm — `api/db.py` does not exist. Verified with `ls`. |
| **dispatches / broadcasts / hex_visits have no DB helpers** | Accurate but intentional — Phase 2/3 surface. Will get helpers when those endpoints land. |

## Hand-off notes for follow-ups

- **Hazard severity mapping for hex-mark findings**: `POST /field/findings` with `kind='hazard'` currently creates a `hazards` row with `kind='other'` and `severity='caution'`. The two enums don't line up 1:1 (finding kinds are about what was observed; hazard kinds are about danger source). If you want operator-selectable severity from the field, the cleanest path is to extend `FindingRequest` with an optional `hazard_severity: Optional[HazardSeverity]` that's passed through when `kind='hazard'`.
- **Per-mission callsign collision UX**: 409 returns a clear message, but the app should probably probe for available callsigns or show a small list rather than letting the user guess.
- **Hex-tap impassable / POI flags**: spec §42 lists these alongside danger as "data model supports it". Currently `/field/findings` only sets `flag_danger` for `kind='hazard'`. If you want `flag_impassable` and `flag_poi` to be tap-settable, simplest extension is to add `'impassable'` and `'poi'` as legal `findings.kind` values (one-line migration: drop and recreate the CHECK constraint) and branch on those in the route the same way we branch on `'hazard'`.

## Verification

Full E2E run against a fresh DB passed:

- POST /missions → 396 segments, 4964 hex cells (hexagons, 6 unique vertices each), 2 init-time hazards.
- POST /missions/join → joiner gets `current_mission_id` set.
- Duplicate same-mission callsign → 409.
- POST /field/ping → 200 for both creator and joiner.
- GET /field/me → returns the joiner's `mission_id`.
- POST /field/findings:
  - `kind='clue'` with description → 201, sets `flag_clue` on containing hex.
  - `kind='footprint'` with description=null → 201 (F-2 verified).
  - `kind='hazard'` → 201 + hazards row inserted + `flag_danger=1` on containing hex (hex-marking verified; 143 cells flagged because the hex's hazard polygon happens to intersect ~143 hex_cells given the small-hex tiling — works as intended).
  - `kind='bogus'` → 422 (F-3 Literal verified).
- POST /missions with `area_geojson` as MultiPolygon → 422 (MI-1 verified).

End-state DB summary: `flag_clue=3`, `flag_danger=143` (across the mission's hex grid).
