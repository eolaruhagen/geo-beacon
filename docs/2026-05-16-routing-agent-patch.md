# Routing-Agent Worker Patch Spec

**Status:** Ready to build. No new write skills, no schema changes, no app changes.

**Companion to:** `docs/2026-05-16-routing-agent.md` (design), `docs/routing-agent-implementation.md` (gap analysis).

**Decision made:** reuse the existing `dispatch_searcher` as the dispatch path instead of building `dispatch_to_cell`. We accept the trade-offs (segment ownership thrash on collision, broadcast spam, fake `sweep_type="hasty"`) because we will not have segment collisions at demo scale and we don't have time to refactor.

---

## Goal

Stand up the routing agent end-to-end. Worker runs every 60s. For each active mission, for each volunteer needing dispatch, the worker:

1. Builds a 10×10 local-view payload centered on the volunteer
2. Calls the LLM with a `dispatch(col, row, reasoning)` tool bound to that volunteer
3. Translates the LLM's chosen cell → hex_id → containing segment_id
4. Completes the volunteer's existing dispatch (so the existing `dispatch_searcher` check passes), then calls `dispatch_searcher` with the hex centroid as `entry_lat/lon`

Phone polls `/field/me` and renders the new pin. No app-side changes needed — the dispatch row shape is identical to today's `/recall`-style dispatch.

## Non-goals

- Adding `dispatch_to_cell` (skipped per team decision)
- Schema migration (none needed)
- App-side changes
- Multi-volunteer coordination beyond what falls out of the per-volunteer pre-compute (other volunteers in view discourage overlap; segment-level coordination is out)
- MCP integration (worker runs in-process)
- Re-dispatch on arrival or on finding (v1: time-based 60s tick only)

## Architecture

```
cron every 60s  →  workers/agent.py main()
                       │
                       ├─ for each active_mission:
                       │   └─ for each volunteer needing dispatch:
                       │       │
                       │       ▼  build_dispatch_payload(mission_id, user_id)
                       │       │      returns (view_anchor, prompt_body)
                       │       │
                       │       ▼  build_dispatch_tool(mission_id, user_id, view_anchor)
                       │       │      returns a closure: dispatch(col, row, reasoning)
                       │       │
                       │       ▼  invoke_llm(SYSTEM_PROMPT, prompt_body, tools=[dispatch_tool])
                       │       │
                       │       ▼  when LLM emits dispatch(7, 3, "..."):
                       │             ├─ translate (col,row) → (lat,lon) → hex_id
                       │             ├─ lookup segment_id from hex_cells
                       │             ├─ if user has active dispatch: complete it
                       │             └─ call dispatch_searcher(...)
                       │
                       ▼  dispatches row inserted; phone sees it next /field/me poll
```

## Files

| Path | Status | Lines |
|---|---|---|
| `agent/payload.py` | new | ~150 |
| `workers/agent.py` | new (re-add, different shape) | ~120 |
| `agent/skills/write.py` | unchanged — `dispatch_searcher` reused as-is | 0 |
| `agent/skills/read.py` | unchanged — `list_searchers`, `active_missions` reused | 0 |
| cron entry | DGX-side `* * * * *` | 1 |

---

## File 1 — `agent/payload.py`

### Public surface

```python
def build_dispatch_payload(
    mission_id: int,
    user_id: int,
) -> tuple[dict, str]:
    """
    Returns (view_anchor, prompt_body).

    view_anchor = {
        "center_lat": float,    # volunteer's current ping lat
        "center_lon": float,    # volunteer's current ping lon
        "cell_size_m": 15.0,    # local-view cell edge in meters
        "grid_size": 10,        # 10×10
    }

    prompt_body = the user-message string per docs/2026-05-16-routing-agent.md.
    """
```

### Steps

**1. Resolve volunteer position.**

Call `list_searchers(mission_id)` (existing skill). Find the row where `id == user_id`. Take `latest_ping.lat / latest_ping.lon` as the view center.

If no `latest_ping`, **raise**. The worker treats this as "skip this volunteer this tick" — no point dispatching someone who hasn't pinged.

**2. Build the local-view grid.**

Local frame: 10×10 square grid, 15m per cell, centered on the volunteer (volunteer always sits at `(col=5, row=5)`).

For each `(col, row)` in `0..9 × 0..9`:
```python
m_per_deg_lat = 111_320.0
m_per_deg_lon = 111_320.0 * math.cos(math.radians(center_lat))
dlat_m = -(row - 5) * 15.0   # row 0 is NORTH, so negate
dlon_m = (col - 5) * 15.0
cell_lat = center_lat + dlat_m / m_per_deg_lat
cell_lon = center_lon + dlon_m / m_per_deg_lon
hex_id = hex_cell_id_at(mission_id, cell_lat, cell_lon)   # existing helper
```

This gives a 10×10 array of `hex_id | None`. `None` means the cell is outside the seeded hex grid (off-map).

Cache the array — you'll iterate over it twice (once to render, once for flood-fill).

**3. Resolve the hexes.**

Single query batched against all non-None hex_ids:
```sql
SELECT id, flag_searched, flag_clue, flag_impassable, is_water, is_building,
       flag_danger, segment_id
FROM hex_cells
WHERE id IN (...)
```

Build `hex_props_by_id: dict[int, dict]` for fast lookup.

**4. Render the ASCII map.**

```python
def cell_symbol(hex_props, is_volunteer, is_other_volunteer, is_pls):
    if is_volunteer:               return "@"
    if is_other_volunteer:         return "v"
    if is_pls:                     return "P"
    if hex_props is None:          return " "        # off-map
    if hex_props["flag_clue"]:     return "!"
    if (hex_props["flag_impassable"]
        or hex_props["is_water"]
        or hex_props["is_building"]):
                                   return "#"
    if hex_props["flag_searched"]: return "o"
    return "."
```

Render with column header row (`     0 1 2 3 4 5 6 7 8 9`) and row labels (`   0 . . . . . . . . . .`).

`is_volunteer` is always True at `(5, 5)`. `is_other_volunteer` requires looking up other searchers' latest pings; see step 5d. `is_pls` is True for any cell containing the mission's `pls_lat/pls_lon`.

**5. Compute facts.**

In this order, appending each non-empty line:

**5a. PLS bearing/distance.** From `missions.pls_lat/pls_lon`. If PLS lies inside the view bbox (already rendered as `P`), skip the line. Otherwise:
```
- Subject's last known position: 240 m NW
```
Use 8-direction compass (N, NE, E, SE, S, SW, W, NW). Distance via `_haversine_m`.

**5b. Nearest clue.** Query the findings table:
```sql
SELECT f.id, f.ts, f.lat, f.lon, f.kind
FROM findings f
JOIN hex_cells h ON h.id = f.hex_id
WHERE f.mission_id = ? AND h.flag_clue = 1
ORDER BY f.ts DESC LIMIT 50
```
Pick the nearest by haversine. Format:
```
- Nearest clue: 47 m SE, reported 4 min ago
```
If clue is in view, you can either skip (already rendered as `!`) or keep the line for emphasis. Recommend keep — the model benefits from the staleness signal.

**5c. Largest unsearched cluster in view.** Flood-fill the 10×10 array treating `.` as unsearched-and-traversable and `#` as wall. Find the largest connected component. Report:
```
- Largest unsearched cluster in view: 24 cells, centered at (8, 1) — NE
```
Compute the centroid as `(round(mean(cols)), round(mean(rows)))`. Bearing from the view center.

**5d. Other volunteers in view.** From the `list_searchers` call in step 1, for each non-self searcher: find their latest_ping, check if its (lat, lon) falls in any view cell. If yes, render that cell as `v` in the grid AND note in facts:
```
- Other volunteers in view: BRAVO at (3, 7)
```
If close-but-outside-view (within 1 view-width), note bearing + distance:
```
- Other volunteers nearby: BRAVO 180 m E
```

**5e. Impassable description.** Only if `#` cells form a cluster larger than 3:
```
- Impassable area: bottom-right, rows 8–9 columns 5–7
```
For small/scattered impassable cells, omit — the ASCII speaks for itself.

**6. Compose.**

```python
return (
    view_anchor,
    "\n".join([
        f"Volunteer: {callsign}",
        "",
        "Map:",
        render_grid(grid, symbols),
        "",
        "Facts:",
        *fact_lines,
    ])
)
```

Target: ~200 tokens. If you're hitting 300+, your facts block has bloated — trim.

---

## File 2 — `workers/agent.py`

### Constants

```python
SYSTEM_PROMPT = """..."""              # exactly from docs/2026-05-16-routing-agent.md
RE_DISPATCH_INTERVAL_S = 60            # re-dispatch if active dispatch older than this
```

### Helpers

```python
def needs_dispatch(searcher: dict, now: int) -> bool:
    """True if the searcher has no active dispatch, or their current one is
    older than RE_DISPATCH_INTERVAL_S seconds."""
    d = searcher["active_dispatch"]
    if d is None:
        return True
    return (now - int(d["issued_ts"])) >= RE_DISPATCH_INTERVAL_S
```

```python
def translate_local_to_world(view_anchor: dict, col: int, row: int) -> tuple[float, float]:
    """Inverse of payload.py's grid math. Returns (lat, lon)."""
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(view_anchor["center_lat"]))
    dlat_m = -(row - 5) * view_anchor["cell_size_m"]
    dlon_m = (col - 5) * view_anchor["cell_size_m"]
    lat = view_anchor["center_lat"] + dlat_m / m_per_deg_lat
    lon = view_anchor["center_lon"] + dlon_m / m_per_deg_lon
    return lat, lon
```

### The dispatch tool closure

```python
def build_dispatch_tool(
    mission_id: int,
    user_id: int,
    view_anchor: dict,
):
    def dispatch(target_col: int, target_row: int, reasoning: str):
        # 1. Translate local → world
        lat, lon = translate_local_to_world(view_anchor, target_col, target_row)
        hex_id = hex_cell_id_at(mission_id, lat, lon)
        if hex_id is None:
            raise ValueError(
                f"Cell ({target_col}, {target_row}) is outside the hex grid"
            )

        # 2. Look up containing segment + hex centroid (single query)
        with session() as conn:
            row = conn.execute(
                """
                SELECT segment_id,
                       Y(Centroid(geom)) AS hex_lat,
                       X(Centroid(geom)) AS hex_lon
                FROM hex_cells WHERE id = ?
                """,
                (hex_id,),
            ).fetchone()
        segment_id = row["segment_id"]
        hex_lat = float(row["hex_lat"])
        hex_lon = float(row["hex_lon"])

        # 3. Complete any active dispatch so dispatch_searcher's check passes
        with session() as conn:
            active = _active_dispatches(conn, mission_id, user_id)
            for a in active:
                conn.execute(
                    """UPDATE dispatches
                       SET status = 'completed', completed_ts = ?
                       WHERE id = ?""",
                    (int(time.time()), a["id"]),
                )

        # 4. Now call the existing dispatch_searcher path
        return dispatch_searcher(
            user_id=user_id,
            segment_id=segment_id,
            sweep_type="hasty",   # routing agent has no opinion; demo-acceptable
            instruction=f"Move to grid ({target_col}, {target_row}).",
            reasoning=reasoning,
            entry_lat=hex_lat,
            entry_lon=hex_lon,
            mission_id=mission_id,
        )

    return dispatch
```

### Main loop

```python
def run_once() -> int:
    now = int(time.time())
    for mission in active_missions():
        mid = int(mission["id"])
        for searcher in list_searchers(mid):
            if searcher["role"] != "searcher":
                continue
            if not needs_dispatch(searcher, now):
                continue
            try:
                view_anchor, prompt_body = build_dispatch_payload(mid, int(searcher["id"]))
                dispatch_tool = build_dispatch_tool(mid, int(searcher["id"]), view_anchor)
                invoke_llm(SYSTEM_PROMPT, prompt_body, tools=[dispatch_tool])
            except Exception as exc:
                print(
                    f"[agent] mission={mid} user={searcher['id']}: {exc}",
                    file=sys.stderr,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_once())
```

### `invoke_llm` — LLM client wiring

This is the one place the patch is client-specific. The shape is the same regardless of provider:

```python
def invoke_llm(system: str, user: str, tools: list) -> None:
    """Single LLM call with the dispatch tool registered.

    Whichever client your team has tool-calling working with:

      - Nemotron via OpenAI-compatible API: pass `tools=[{...}]` per OpenAI
        function-calling shape, parse tool_calls in the response, invoke the
        closure with the parsed args.

      - Anthropic SDK: tools=[{name, description, input_schema}], handle
        tool_use content blocks.

      - OpenClaw subprocess: model emits structured output (probably JSON),
        parse it and call the closure. Brittle. Avoid if you have anything
        else working.

    The tool function (`tools[0]` here) is just the closure returned by
    build_dispatch_tool — call it with kwargs from the model's tool call.
    """
    ...
```

The dispatch closure is a plain Python callable — it doesn't care about MCP, JSON-RPC, or any transport. Whatever the LLM client emits as "the model wants to call `dispatch(col=7, row=3, reasoning='...')`" you translate to `tools[0](target_col=7, target_row=3, reasoning='...')`.

Recommended: native tool-calling SDK. Skip the OpenClaw subprocess path unless it's already working.

---

## Imports needed

```python
# agent/payload.py
import math
from api.db import session
from api.db.hex_cells import hex_cell_id_at
from agent.skills.read import _haversine_m, list_searchers
import api.db.missions as db_missions
```

```python
# workers/agent.py
import math
import sys
import time
from api.db import session
from api.db.hex_cells import hex_cell_id_at
from agent.payload import build_dispatch_payload
from agent.skills.read import active_missions, list_searchers
from agent.skills.write import dispatch_searcher, _active_dispatches
```

---

## Test plan

1. **Pre-flight.** Seed mission, two phones join, both ping for 60s. Confirm:
   ```bash
   sqlite3 /home/asus/sqlite/mission.db \
     "SELECT COUNT(*) FROM hex_cells WHERE flag_searched = 1 AND mission_id = 1"
   ```
   should be > 0.

2. **Payload dry-run.** Without involving the LLM:
   ```bash
   python -c "from agent.payload import build_dispatch_payload; \
              anchor, body = build_dispatch_payload(1, 2); \
              print(body)"
   ```
   Eyeball the ASCII grid + facts. The `@` should be at row 5 col 5. Token count should be 150–250.

3. **Closure dry-run.** Without LLM:
   ```python
   from workers.agent import build_dispatch_tool
   from agent.payload import build_dispatch_payload
   anchor, _ = build_dispatch_payload(1, 2)
   d = build_dispatch_tool(1, 2, anchor)
   result = d(target_col=7, target_row=3, reasoning="test")
   print(result)  # should show dispatch_id and segment_name
   ```
   Then check the DB: `SELECT * FROM dispatches ORDER BY id DESC LIMIT 1`. Status='pending', entry_lat/lon set to the hex centroid.

4. **End-to-end one tick.** `python -m workers.agent`. Verify exactly one dispatch row per searcher who needed one.

5. **Cron.** `* * * * * cd /home/asus/geo-beacon && .venv/bin/python -m workers.agent` on the DGX. Watch logs for 3 ticks. Each searcher should get a fresh dispatch each tick (because the 60s threshold matches the tick).

6. **App.** Phone receives the dispatch via existing `/field/me` poll. The pin should land at the hex centroid you can verify via the test in step 3.

## Decisions for the implementer

1. **LLM client.** Whichever your team has tool-calling working with. Patch is client-agnostic.

2. **Re-dispatch trigger.** Patch uses elapsed time since `issued_ts` ≥ 60s. Other options (not in v1): on user arrival within 1 cell of target, on finding logged in view, on SOS. Add later if the time-based version feels wrong.

3. **What `instruction` should say.** Current placeholder: `"Move to grid ({col}, {row})."` — meaningless to the volunteer. Better might be a short rendering of the reasoning string. Easy to swap.

4. **What to do if the LLM picks an off-grid cell.** Closure raises; worker logs and skips. The next 60s tick gets another chance. If it happens consistently, the prompt is wrong (model isn't reading the map), not the code.

5. **Whether to keep the broadcast row.** `dispatch_searcher` writes a broadcast row per call → 2880 rows per 24h with 2 searchers. Cosmetic. Leave it; clean up post-demo if anyone notices.
