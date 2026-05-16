# 2026-05-16 — Hazards semantics: buildings & roads are no longer hazards

## The change

`scripts/seed_hazards.py` no longer inserts buildings or roads into the
`hazards` table. Only **water** (critical) and **steep terrain ≥ 30°**
(cliff, caution) are seeded as hazards now.

Buildings and roads are still surfaced — they live on `hex_cells`:
- OSM building polygons → `hex_cells.is_building = 1`
- OSM road linestrings → `hex_cells.has_road = 1` (set in `fetch_terrain.py`)

Renderers should style those flags however they want. They don't enter the
POA-penalty or `flag_danger` path.

## Why

The previous behavior treated every OSM building and road as a
`severity='caution'` hazard, which had two downstream effects:

1. `segments.apply_hazard_penalty` multiplied any intersecting segment's
   POA by **0.3** — a ~70% knockdown of search probability for that area.
2. `rasterize_hazard_to_hex_flags` set `flag_danger = 1` on every hex the
   building or road polygon clipped.

That doesn't match how SAR actually models terrain. A barn or a fire road
in a search area is *information* — searchers walk around it, or it's
already cleared because someone checked the doors — but the area is still
fundamentally searchable. Treating it as a danger zone biases the agent
away from areas it should still be planning sweeps through.

Hazards now means **"risky to enter."** Water (drowning) and cliffs (fall
risk) qualify. A locked shed doesn't.

## What stays the same

- `hex_cells.is_building` and `hex_cells.is_water` are still set in the
  same place (the two `UPDATE hex_cells SET …` blocks at the bottom of
  `seed_hazards`). No code changes there.
- Water still produces a `critical` hazard, which zeros out POA on
  intersecting segments. That's intentional and correct.
- Cliff detection (slope ≥ 30°) is unchanged.
- `apply_hazard_penalty`, `rasterize_hazard_to_hex_flags`, and
  `state.geojson` rendering — none touched.

## What got removed

- `ROAD_BUFFER_M`, `BUILDING_BUFFER_M`, `_buffer_deg`, `_mission_centroid_lat`
  helpers from `seed_hazards.py` — only used by the dropped logic.
- `counts["road"]` and `counts["building"]` keys from the function's
  return dict. No callers depended on them (`grep` clean).
- `import math` — no longer used.

## Verification

```
PHASE 1 — POST /missions
  status=201
  mission_id=1 segs=99 hex=1241 hazards=1
PHASE 2 — hazards table contents
  hazards by (kind, severity): {('water', 'critical'): 1}
PHASE 3 — hex_cells flags
  total=1241 is_building=0 is_water=4 flag_danger=9
PHASE 4 — segment POAs sum to ~1.0 (renormalized)
  n_segments=99 sum_poa=1.000000
PHASE 5 — GET /mission/state.geojson works
  status=200
```

Only `water` shows up in the hazards table after mission init; building /
road kinds are absent. `is_water` still propagates feature flags. POA
renormalization still works.

## Follow-ups (separate PRs, not in this one)

- **Segments-as-hexagons.** `seed_segments.py` currently emits a square grid;
  switching it to a flat-top hex grid (matching `fetch_terrain.py`) is a
  ~30-LoC rewrite plus a touch to `seed_hex_cells.py` for parent-hex
  assignment. No schema or API changes. Purely a tessellation/visual
  improvement.
- **Optional building "search difficulty" nudge.** If we later want
  buildings to slightly *de-prioritize* a hex without making it a no-go,
  the right place is a small additive factor on `hex_cells` rather than a
  hazard row.
