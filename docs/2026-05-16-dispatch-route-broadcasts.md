# 2026-05-16 — Hex segments, dispatch lifecycle, route hints, broadcasts

Branch: `segments-as-hex`. Three logical changes stacked as three commits
on top of `main`. App contract for all new endpoints lives in
`docs/CONTRACTS.md` (kept as the single source of truth).

## 1. Segments are now hexagons

`scripts/seed_segments.py` was rewritten to emit a **105 m flat-to-flat
flat-top hex grid** instead of a 100 m square grid. Origin is the mission
bbox SW corner, odd-q offset coordinates. Segments are named
`S-r{NN}-c{NN}` so radio callouts map directly to grid position
(e.g. `S-r04-c12`).

`scripts/seed_hex_cells.py` lost its bbox-grid fast path (square-only) and
now uses **SpatiaLite `ST_Contains`** to assign each fine hex to its
parent segment. The spatial index on `segments.geom` keeps this at
~O(log N) per lookup.

**Zero schema or API changes.** `segments.geom` was already `POLYGON`;
SpatiaLite doesn't care if it's 4-sided or 6-sided. `state.geojson`
serializes whatever's there. POA/POD math is shape-agnostic.

Side effects worth knowing:

- Segments are not clipped to `area_geom` — hexes covering the bbox stay
  even if they stick out beyond the user-drawn polygon (matches the
  previous square behavior; keeps the search area generous).
- Each segment area is exactly ~9,547 m² (theoretical 9,545 plus
  projection rounding).
- Every segment has 6 neighbors instead of 4 (free benefit for any future
  "sweep adjacent sector" logic; not used today).

## 2. Dispatch lifecycle endpoints

`MeResponse.active_dispatch` and `.segment_geojson` (previously stubbed
`null`) are now populated. Three new endpoints drive the dispatch state
machine from the searcher side:

```
pending ─ack─► acked ─start─► in_progress ─complete─► completed
```

| Method | Path                              | Required prev status | Side effects                              |
| ------ | --------------------------------- | -------------------- | ----------------------------------------- |
| POST   | `/field/dispatch/{id}/ack`        | `pending`            | sets `acked_ts`                           |
| POST   | `/field/dispatch/{id}/start`      | `acked`              | sets `started_ts`; user.status → `on_segment` |
| POST   | `/field/dispatch/{id}/complete`   | `in_progress`        | sets `completed_ts`, `completion_notes` (optional `{notes?: str}` body); user.status → `standby` |

**Auth**: dispatch must belong to the calling user → else 403 (without
leaking the actual owner). Out-of-order transitions return **409** with
the exact required status in the detail string. Unknown id returns
**404**.

**Schema change**: added `dispatches.completion_notes TEXT` (nullable)
inline in `migrations/001_init.sql`. Pre-launch; dev `reset-db.sh`
rebuilds.

## 3. Route hints — `/field/me/route?segment_id=X`

Snap-to-trail waypoints from the searcher to their target segment.
Matches spec §13 `query_route`: snap only, no graph routing.

```
start  = latest ping for (user, mission)  →  fallback: mission.PLS
target = active_dispatch.entry_lat/lon (if for this segment)
                                          →  fallback: segment centroid

with trails: [start, snap(start), snap(target), target]   snapped=true
no trails:   [start, target]                              snapped=false
```

SpatiaLite `ClosestPoint(line, point)` against `osm_features` where
`kind='trail'`, ordered by `Distance`. New helper:
`api/db/routing.snap_point_to_nearest_trail`.

## 4. Broadcasts (this commit)

### `MeResponse.recent_broadcasts` is populated

Inline payload in `/field/me`, capped to the **most recent 5** visible
broadcasts. Cheap enough for the 5 s poll. Use this for the alert banner.

### `GET /field/announcements?since={ts}` — full incremental feed

Watermark-paginated. App stores `cursor_ts` from each response and
re-polls with `?since=cursor_ts`. Empty batch echoes the `since` value
back so the cursor doesn't slide backward.

### RLS-like scope policy (read this)

`broadcasts.scope` is `'all'` or `f'user:{user_id}'`. SQLite has no
row-level-security, so the policy is enforced in code:

> Every read passes through
> `api/db/broadcasts.visible_broadcasts_for_user(user_id, mission_id, …)`,
> which filters on
> `mission_id = ? AND (scope = 'all' OR scope = f'user:{user_id}')`.

Both endpoints (`/field/me` inline + `/field/announcements`) use that
helper. **Do not query the `broadcasts` table directly from a route
handler** — that's how the policy gets accidentally bypassed.

Verified end-to-end (`smoke_broadcasts.py`):

- Two users in the same mission. Each gets a targeted broadcast
  (`user:alpha` / `user:bravo`) and there are two `all`-scoped ones.
- Alpha sees the `all` broadcasts + her own targeted one. Bravo
  symmetrically. **Neither sees the other's targeted message** —
  confirmed across `/field/me` AND `/field/announcements`.
- `since=ts` watermark works; future `since` returns empty + echoes
  cursor; orphan user (no mission) returns 409; negative `since`
  returns 422.

When you add a new scope type later (e.g. `'team:{id}'`), update the
WHERE clause in `visible_broadcasts_for_user` AND the contract section
in `docs/CONTRACTS.md` in the same change.

## Migrations / DB resets

- `migrations/001_init.sql` gained one column: `dispatches.completion_notes TEXT`.
  No other schema changes.
- Run `./dev/reset-db.sh` on dev. The DGX picks up the change via
  `respawn-workers.sh` re-applying migrations (the `applied_migrations`
  table prevents double-applying, but a pre-launch schema edit may need a
  full DB wipe — coordinate with the team).

## Verification summary

All three commits ship with throwaway smoke tests run against a fresh
DB. Phase-by-phase output is in the commit messages. Highlights:

| Smoke                   | Phases | Notable assertions                                                                                                  |
| ----------------------- | ------ | ------------------------------------------------------------------------------------------------------------------- |
| `smoke_hex_segments.py` | 7      | 132 hex segments, every fine hex has a non-null `segment_id`, area = 9547.9 m², POA renormalizes to 1.0             |
| `smoke_dispatch.py`     | 15     | Happy path + 409 on every out-of-order transition, 403 cross-user, 404 unknown id, `completion_notes` persisted     |
| `smoke_route.py`        | 8      | Bee-line fallback, perpendicular snap within 1e-5°, dispatch entry overrides centroid, 404/409 edge cases           |
| `smoke_broadcasts.py`   | 9      | RLS scope enforced both surfaces, inline cap=5, watermark math, 409 orphan, 422 negative since                       |

## What this PR does NOT include

- Agent skills (`dispatch_searcher`, `recall_searcher`, `broadcast`,
  `flag_hazard`). The endpoints are usable today only via direct DB
  inserts mimicking what those skills will do — that's how the smoke
  tests fixture them.
- `/field/sos`, `/mission/timeline`, `/mission/agent_memory` — separate
  PRs, tracked in spec §13.
- Tightening the `dispatches.completion_notes` write path (e.g.
  promoting notes to a `findings`-like row for the agent to see) — the
  column is there; how the agent consumes it is a future decision.
