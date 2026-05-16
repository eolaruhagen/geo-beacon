# 2026-05-16 — Audit fixes (PR #5 follow-up to PR #4)

Follow-up to PR #4 (schema validation). A read-only audit pass turned up three real auth issues and a couple of latent bugs. This branch fixes everything in the audit report through the "polish" line. Branch: `audit-fixes`.

Each fix is tied to its audit code.

## Security

| Code | Severity | Where | What changed |
|---|---|---|---|
| **AUTH-1** | HIGH | `api/routes/mission.py` | `GET /mission/state.geojson?mission_id=X` was returning the full state of any mission to any authenticated token holder. **Pre-fix**: a stolen bearer token (e.g. from the `/field/me` leak — see PYD-1) could enumerate `mission_id` integers and scrape every other mission's segments, searchers, findings, hazards from the demo deploy. Now requires that the caller is either the mission creator (`missions.created_by_user_id`) or a current member (`users.current_mission_id == mission_id`). 403 otherwise. |
| **PYD-1** | MEDIUM (HIGH with ngrok HTTP) | `api/schemas.py` + `api/routes/field.py` | `MeResponse.user: Any` was returning the raw user dict on every `/field/me` poll — including `bearer_token` and `phone`. Polled every 5s, the token was effectively broadcast over the wire. Added a `UserPublic` model with explicit allowed fields (`id, display_name, callsign, role, status, current_mission_id`) and `extra="ignore"`; `field_me` now projects through it. Bearer token and phone are dropped from the response. |
| **AUTH-2** | MEDIUM | `api/db/missions.py` | `active_mission_id_for_user` had a "single active mission" global fallback: if a user had no `current_mission_id` and no created mission and no pings, but exactly one mission was live, they'd silently land in that mission. In the hackathon deploy (always one active mission), this meant any authenticated user could ping / log findings into a stranger's mission. Removed both the global fallback and the created-by/pings inference; the only authoritative source now is `users.current_mission_id`. NULL means "no active mission" → 409 at the route. |

## Correctness

| Code | Severity | Where | What changed |
|---|---|---|---|
| **BUG-4** | MEDIUM | `api/routes/missions.py` | `POST /missions` was wrapping every seeding step (`fetch_terrain`, `seed_segments`, `seed_hex_cells`, `seed_hazards`) in `try/except` with only a `logger.warning`. A mission could end up `status='active'` with `n_segments=0`, then `POST /field/findings` would 422 forever on the hex resolution. Added a guard: after seeding, if `n_segments == 0 or n_hex == 0`, raise 500 immediately. Mission row is left in `status='planning'` (never activated), so it's safe to retry with a new area. |

## Cleanup

| Code | Severity | Where | What changed |
|---|---|---|---|
| **SPEC-3** | LOW | `api/schemas.py:113`, `api/db/users.py:20`, `api/db/missions.py:93` | Three comments referenced `migrations/004_user_mission_and_validation.sql`, which was deleted when the schema was inlined into 001/002 last PR. Updated to point at the real migration files. |
| **BUG-5** | LOW | `scripts/smoke_test_db.py` | Deleted. The file referenced `api.db.terrain` (renamed to `api.db.osm`), `api.db.gate` (removed when the agent queue was dropped), `create_mission()` without `created_by_user_id`/`join_code` (signature changed in PR #3), and `bulk_insert_terrain_cells`/`enqueue_trigger` (both gone). Zero callers in the repo. The TestClient-based E2E we use now covers the same surface and stays in sync with the real routes. |

## Verification

Full E2E run against a fresh DB exercises every fix end-to-end:

```
PHASE 1 — happy path regression
  POST /missions: 201 mid=1 segs=396 hex=4964
  POST /missions/join: 201
PHASE 2 — PYD-1 bearer_token leak check
  /field/me.user keys: ['id','display_name','callsign','role','status','current_mission_id']
  no bearer_token, no phone
PHASE 3 — AUTH-2 no-global-fallback
  orphan /field/ping: 409          (was: 200, would have landed in mission A)
  orphan /field/me.mission_id: None
  orphan /field/findings: 409
  orphan /mission/state.geojson: 404
PHASE 4 — AUTH-1 cross-mission read protection
  tok_A reading mission B: 403     (was: 200, full state leak)
  tok_B (creator) reading own:    200
  tok_A reading own:              200
  joiner reading own:             200
  joiner reading mission B:       403
  tok_A omitting mission_id:      200 (auto-resolves to A)
PHASE 5 — BUG-4 guard present (source inspection)
PHASE 6 — cleanup verified (no stale 004 refs, smoke_test_db.py gone)
```

(One harmless artifact: the test process exits 139 during interpreter teardown — SpatiaLite's connection-cleanup is occasionally segfaulty on macOS Python 3.11. All assertions complete before that point. Doesn't affect production.)

## Audit items intentionally not fixed in this PR

| Code | Why deferred |
|---|---|
| **PYD-2** | Lower-bound checks on `accuracy_m` / `speed_mps` — polish only. Add when phone integration shakes out the real value ranges. |
| **PYD-3** | `display_name` `max_length` — same bucket. |
| **PYD-4** | Misleading validator error wording when only `lat` is sent. Cosmetic; the outcome is still a 422. |
| **BUG-1** | `state.geojson` track query ordering relies on SQLite-observed behavior. Worth re-verifying when the search-track rendering gets used in earnest. |
| **BUG-2** | Bulk-insert helpers don't `ROLLBACK` on partial failure. Real risk but contained — `with session()` closes the connection on exception so visible damage is small. Wrap in try/except when we land a proper test harness for these helpers. |
| **BUG-3** | `cur.lastrowid` typed `Optional[int]` passed into an `int` field. Practically never None after a successful INSERT. Type-tighten when we move to mypy strict. |
| **BUG-6** | Typo `feat.get("geom_geojson") or feat.get("geom_geojson")` in mock data path. Harmless. Fold into the next pass on `fetch_terrain.py`. |
| **BUG-7** | Silent drop of unclosed OSM building geometries. Add a `logger.debug` line when we touch that block next. |
| **AUTH-3** | `join_code = secrets.token_hex(3)` is 24 bits, brute-forceable for a long-running mission on a public ngrok URL. Hackathon-acceptable; widen to 8+ hex chars before any non-demo deploy. |
| **SPEC-1** | Dispatch / sos / announcements / timeline endpoints not yet implemented. Tracked separately — next PR will land the dispatch endpoints (GET `/field/me` fill-in + POST `/field/dispatch/{id}/{ack,start,complete}`). |
| **SPEC-2** | `MeResponse.active_dispatch` is now `Optional[Any]` instead of hardcoded `None`, so the field type can be tightened to `Optional[ActiveDispatch]` when the dispatch model lands — no wire-shape break. |

## Upgrade path

Same as PR #4: wipe your local `dev/data/mission.db` and re-apply migrations from scratch with `./dev/reset-db.sh` (or the DGX equivalent: nuke `/home/asus/sqlite/mission.db` and let `respawn-workers.sh` re-apply). No new migrations in this PR — only application-layer changes.
