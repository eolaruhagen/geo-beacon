# Dispatch Agent — Prompt + Pre-compute Spec

**Status:** Design locked, not yet built. Replaces §10 "Mission Brief" in `superpowers/specs/2026-05-15-sar-mission-control-design.md` — the brief is dropped in favor of the per-volunteer local-view prompt below.

## What this is

The agent dispatches **one volunteer at a time** by picking a single nearby cell for them to walk to. It does **not** plan whole missions, assign whole segments, or coordinate the team holistically. Each invocation is one volunteer, one target cell, one short reasoning string.

The model receives a small local view of the world around that volunteer plus a tight block of deterministically pre-computed facts. The pre-compute does ~80% of the work; the model breaks ties and produces a human-readable rationale.

## Architecture

```
                                                 ┌──────────────────────────────┐
                                                 │  build_dispatch_payload()    │
   cron / SOS / commander override ─────────────▶│  - crops 10×10 local view    │
                                                 │  - computes facts            │
                                                 │  - returns prompt body       │
                                                 └─────────────┬────────────────┘
                                                               │
                                                               ▼
                                                 ┌──────────────────────────────┐
                                                 │  nemoclaw / openclaw         │
                                                 │  with `dispatch` tool        │
                                                 └─────────────┬────────────────┘
                                                               │ dispatch(col,row,reason)
                                                               ▼
                                                 ┌──────────────────────────────┐
                                                 │  dispatch handler            │
                                                 │  - local→world coord xform   │
                                                 │  - INSERT dispatches row     │
                                                 │  - notifies the app          │
                                                 └──────────────────────────────┘
```

One LLM call per volunteer per tick. The dispatcher runs **per volunteer**, not globally — if Alpha, Bravo, and Charlie all need a target, that's three calls.

## Cadence

Each cell is 15 m × 15 m → the 10×10 view covers 150 m × 150 m. A walking volunteer crosses that in ~2–5 minutes, so dispatch is **incremental steering**, not one-shot assignment.

- Cron tick: every **60 s** (matches the existing design's agent cadence).
- Inline invocation: on `/field/sos`, on volunteer arrival at previous target, on new finding logged within a volunteer's view.
- One LLM call per stale-target volunteer per tick.

## System prompt (cached)

Send this once. It does not vary per call.

```
You dispatch one search-and-rescue volunteer per call. The user message
contains a 10×10 local view centered on the volunteer plus pre-computed
facts about their surroundings. Pick the best cell to send them to and
call dispatch().

Map symbols:
  .  unsearched
  o  searched
  #  impassable
  P  subject's last known position
  !  clue reported by a volunteer
  @  the volunteer you are dispatching (always at column 5, row 5)
  v  another volunteer

Coordinates: column 0–9 left-to-right, row 0–9 top-to-bottom. North is up.
Each cell is 15 m × 15 m.

Prefer cells that:
1. Are reachable (no # blocking the direct path)
2. Are unsearched
3. Are near a recent clue or the subject's last known position
4. Don't duplicate another volunteer's coverage

Call dispatch(target_col, target_row, reasoning). Reasoning ≤ 15 words.
```

## User message template

Built per call by `build_dispatch_payload()` from current DB state. Drop any "Facts" line that doesn't apply — empty entries are noise.

```
Volunteer: {callsign}

Map:
     0 1 2 3 4 5 6 7 8 9
   0 . . . . . . . . . .
   1 . . . . . . . . . .
   2 . . . . . o o o . .
   3 . . . o o o o o . .
   4 . . o o o o o o o .
   5 . . o o o @ o o o .
   6 . . o o o o o ! . .
   7 . . . o o o o . . .
   8 . . . . . . . . . .
   9 . . . . . # # # . .

Facts:
- Subject's last known position: (5, 3) — 30 m NW
- Nearest clue: (7, 6) — 47 m SE, reported 4 min ago
- Largest unsearched cluster in view: 24 cells, centered at (8, 1) — NE
- Impassable area: bottom-right, rows 9 columns 5–7
- Other volunteers in view: none
```

Target size: **~200 tokens per call**. The whole transaction (system + user + tool call response) should land under 600 tokens.

## Pre-compute pipeline

This is the load-bearing part. The Python function `build_dispatch_payload(mission_id, user_id)` returns the user-message string above. It computes:

| # | Field | Source | Notes |
|---|---|---|---|
| 1 | 10×10 crop | `hex_cells` table around `volunteer.last_ping.geom` | `@` always at (5, 5). Map each hex's flags + searched-state to one symbol. |
| 2 | PLS bearing/distance | `missions.pls_lat`, `pls_lon` | Compute compass bearing and meters from volunteer's current position. Skip the line if PLS is in view (already visible as `P`). |
| 3 | Nearest clue | `hex_cells.flag_clue=1` ordered by distance | Include `reported N min ago` from `hex_cells.flags_updated_ts`. Skip if no clues anywhere in mission. |
| 4 | Largest unsearched cluster | flood-fill on `unsearched ∧ traversable` within view | Report cell count + centroid in local coords + bearing. |
| 5 | Impassable description | `hex_cells.flag_impassable=1` or `is_water=1` or `is_building=1` | One short sentence. If complex, omit and let the ASCII speak. |
| 6 | Other volunteers in view | latest `pings` for users in same mission | Only those whose current hex falls in the 10×10. If close-but-outside, mention bearing/distance to discourage overlap. |

**Cell → symbol mapping** (precedence top-down):

```python
def cell_symbol(hex_cell, is_volunteer_here, is_other_volunteer_here, is_pls_here):
    if is_volunteer_here:                   return "@"
    if is_other_volunteer_here:             return "v"
    if is_pls_here:                         return "P"
    if hex_cell.flag_clue:                  return "!"
    if hex_cell.flag_impassable or hex_cell.is_water or hex_cell.is_building:
                                            return "#"
    if hex_cell.searched:                   return "o"
    return "."
```

`searched` is derived from "any ping from any volunteer has fallen inside this cell." That's the spatial-worker job (already designed in §8 of the SAR design doc). If the spatial worker isn't ready, fall back to "any pings from the *current* volunteer's track over the last 30 min."

## Tool signature

The agent calls one tool, exposed via openclaw:

```python
def dispatch(target_col: int, target_row: int, reasoning: str) -> None:
    """
    target_col, target_row: 0-9, in the local view's coordinate frame.
    reasoning: <=15 words, written for humans (appears in dispatch UI + journal).
    """
```

The dispatch handler:
1. Translates `(target_col, target_row)` to a world hex_id using the view's anchor (which the dispatcher stored before the LLM call).
2. INSERTs a `dispatches` row referencing the target hex.
3. The app's `/field/me` poll picks up the new dispatch and renders it on the next refresh.

**The model never sees absolute world coordinates.** This is deliberate: LLMs are bad at multi-digit arithmetic, and we own the translation.

## Ownership

| What | Owner | Lives in | Status |
|---|---|---|---|
| `hex_cells.searched` column + spatial worker that maintains it | Eric | `workers/spatial.py` (new) + migration | **Blocker** — pre-compute can't be honest without this |
| `build_dispatch_payload()` | Agent person | `agent/payload.py` (new) | New work |
| openclaw integration + `dispatch` tool | Agent person | `workers/agent.py` (new — does not exist yet) | New work |
| Dispatch handler + endpoint | API person | `api/routes/admin.py` or `api/routes/field.py` | `dispatches` table designed, endpoint not built |
| App rendering of incoming dispatch | App person | `sar-app/app/mission.tsx` | Already polls `/mission/state.geojson`; needs to read dispatch from `/field/me` |
| Mission seed with PLS coords | Whoever runs the demo | `scripts/seed_mission.py` | Already exists; verify `pls_lat`/`pls_lon` populated |

## What we deliberately did NOT include

If a teammate asks "why isn't X in the prompt?" — these were considered and dropped on purpose:

- **Subject narrative** ("12-year-old, red jacket, last seen 90 min ago"). Adds tokens, doesn't change the dispatch decision. Re-add only if we layer a witness-report channel later.
- **Multi-volunteer coordination.** The model dispatches one volunteer per call. Coordination across volunteers happens *outside* the LLM, via the pre-compute (other-volunteer positions discourage overlap).
- **Whole-mission view.** Per-volunteer local view only. Global state is the dashboard's job, not the agent's.
- **"Briefly describe what you see"** intermediate step. Wasted tokens; we don't read it.
- **POA / POD scoring in the prompt.** The pre-compute already surfaces unsearched-cluster size and bearings — those are the actionable signals. POA math stays in segment-level reasoning, which the dispatcher doesn't do.

## Open questions

1. **Reachability beyond impassable.** Model might pick a cell on the far side of a `#` block. Two options:
   - Trust `/field/me/route` to snap to trail and let pathing fail gracefully if unreachable.
   - Pre-compute "reachable-in-view" as a boolean per cell, render unreachable cells with a different symbol.
   Start with option 1; revisit only if the model picks bad cells in practice.

2. **What does the app show when a dispatch lands?** A pin + a route? Just the pin and let the volunteer walk toward it? TBD with app person.

3. **Re-dispatch trigger.** When does the volunteer's *next* dispatch get computed?
   - On arrival at previous target (server detects, fires inline invocation)
   - On stale target (volunteer has been within 1 cell of target for >5 min)
   - On finding logged in volunteer's view
   - At every cron tick if no dispatch is active

   I'd start with "arrival + every 60s if standby." Cheap to invoke.

4. **Do we even need the LLM here?** A weighted heuristic (nearest unsearched cell, weighted by distance to PLS and recent clues, with overlap penalty) gets ~80% accuracy at zero token cost. The LLM earns its slot on the `reasoning` field (demo-friendly natural language) and on cases where competing priorities are hard to weight (weather, daylight, terrain difficulty). Ship the LLM version; if it doesn't visibly outperform a heuristic on the seeded scenario, swap to a heuristic and pocket the latency.

## Test plan

Done = all six pass.

1. Seed mission, two phones join (Alpha + Bravo). Both ping for 60 s.
2. Force-invoke the dispatcher for Alpha. Verify:
   - User message renders cleanly (correct ASCII, fact lines populated, none truncated)
   - Token count under 300 for the user message
3. Tool call lands. `dispatches` row created. Translation from local (col, row) → world hex matches what the map would show at that view anchor.
4. App receives dispatch via `/field/me` poll. Pin renders on Alpha's map.
5. Run the cron loop for 5 minutes with two volunteers. Verify dispatcher fires every 60 s, picks different cells as the volunteer moves, and never sends both volunteers to the same hex.
6. Drop a clue in Alpha's view (`flag_clue=1` on a nearby hex). Within the next cron tick, the dispatcher should bias Alpha toward that cell. (If it doesn't, the pre-compute is wrong — not the prompt.)

## What to ship in what order

1. **Eric:** `hex_cells.searched` updater (or the 30-min-track fallback). Without this, the map is all `.` and the dispatcher is blind.
2. **Agent person:** `build_dispatch_payload()` standalone — run it against the seeded mission, eyeball the output. Get the pre-compute right *before* wiring the LLM.
3. **Agent person:** openclaw call + dispatch tool. Verify it round-trips end-to-end on one volunteer.
4. **API person:** dispatch handler endpoint, `dispatches` INSERT.
5. **App person:** render incoming dispatch on `mission.tsx` (pin or pin+route).
6. **Demo run.** Two phones, 5-minute timer. See where the agent sends them.

Iterate on the pre-compute first. The prompt is rarely the bottleneck — the data shaping is.
