# Routing Agent — Integration with Existing Skill Layer

**Companion to:** `docs/2026-05-16-dispatch-agent.md`.
**Audience:** the agent person wiring `build_dispatch_payload()` + the LLM call.

## TL;DR

The skill layer in `agent/skills/{read,write}.py` was built for the **§10 Mission Brief** model (one LLM call, whole-mission segment-grain reasoning, ~7 read tools + 7 write tools). The new dispatch-agent spec replaces that model with **per-volunteer hex-grain local-view dispatch** (one LLM call per volunteer, ~200-token prompt, one write tool).

Net effect on the existing layer:
- **Most read skills are unused by the new agent.** Some had a future use in commander UI / dashboards, but mission creation is now manual via seed script + ngrok (no app-side mission UI), and the commander-grade brief loop is deprecated too. So most read skills are unused **period**, not just unused-by-routing-agent.
- **`dispatch_searcher` is the wrong grain** — it dispatches to *segments*, not cells. Need a new sibling: `dispatch_to_cell()`.
- **`agent/brief.py` is dead code, full stop.** The whole brief-creation pipeline (§10 Mission Brief) is deprecated. Routing agent uses `build_dispatch_payload()` instead. No consumer remains for `compose_brief()` — delete the file and unregister `get_mission_brief` from MCP.
- **Schema is already there.** `hex_cells.flag_searched`, `flags_updated_ts`, `searched_by_user_id`, `searched_ts` all exist. `dispatches.segment_id` is nullable and `entry_lat/entry_lon` exist — cell-grain dispatch fits the existing row shape with no migration.

## What exists today

### `agent/skills/read.py` — 9 read tools

| Tool | Grain | Useful for routing agent? | Still has any consumer? |
|---|---|---|---|
| `get_mission_brief()` | whole mission, markdown | No — replaced by `build_dispatch_payload()` | **No — delete** |
| `get_mission_overview()` | mission counts | No — not needed per-volunteer | No (mission UI deprecated) — delete |
| `get_segment()` | segment | No — agent doesn't reason about segments | No — delete |
| `get_searcher()` | one user | **Maybe** — gives ping position | Yes for debugging; keep |
| `get_findings()` | mission, filtered | **Yes** — feeds "nearest clue" | Yes — keep |
| `get_terrain_summary()` | segment | No — segment grain | No — delete |
| `get_uncovered_areas()` | segment ranking | No — needs cell-grain replacement | No — delete |
| `query_route()` | lat/lon pair | No — routing happens app-side via `/field/me/route` | No — delete |
| `recent_events()` | mission | **Yes** — useful for worker gating (when to fire dispatcher) | Yes — keep |

### `agent/skills/write.py` — 7 write tools

With mission creation deprecated and no commander UI, the rule changes from "keep for commander" to "delete unless something still calls it."

| Tool | Routing agent uses? | Still has any consumer? |
|---|---|---|
| `dispatch_searcher(user_id, segment_id, sweep_type, ...)` | No — wrong grain | No commander UI; routing agent uses cell-grain. **Delete** unless retained for hand-debug dispatch via curl. |
| `reassign_searcher()` | No — supersession happens inside `dispatch_to_cell()` | No — delete |
| `recall_searcher()` | No | No commander UI to invoke it — delete |
| `broadcast()` | No | No commander UI; only useful if a future agent escalation path emits them — delete for now |
| `flag_hazard()` | No | Hazards are seeded with the mission (manual script). No runtime caller — delete |
| `update_segment_poa()` | No — cell-grain | No — delete |
| `update_mission_status()` | No | Mission lifecycle handled via seed script + manual DB update — delete |

### `agent/brief.py` — composes the Mission Brief

Entire file is the §10 brief composer. The whole brief-creation pipeline is deprecated (no commander dashboard, no app-side mission creation, routing agent uses `build_dispatch_payload()` instead). **Delete the file.**

### `agent/mcp_server.py` — MCP adapter

Registers all 16 tools above. Per the recommendation below the routing agent shouldn't go through MCP at all, and most of the registered tools have no remaining consumer. After the cleanup: either delete `mcp_server.py` entirely or strip it down to whatever read tools you still want exposed for ad-hoc debugging (probably just `get_searcher` + `recent_events`).

## What the dispatch-agent spec actually needs

Per `docs/2026-05-16-dispatch-agent.md`, the routing agent needs **two functions only**:

1. **`build_dispatch_payload(mission_id, user_id) -> str`** — returns the user-message string (ASCII map + Facts block).
2. **`dispatch(target_col, target_row, reasoning) -> None`** — the model's single write tool. Translates local (col, row) → world hex → INSERT into `dispatches`.

Everything else in the spec (cron loop, openclaw invocation, prompt assembly) is wrapper code around these two.

## Redundancies (vs current skill layer)

| What overlaps | Recommendation |
|---|---|
| `compose_brief()` (`brief.py`) ↔ `build_dispatch_payload()` (new) | Different grain, different consumer. **Keep `compose_brief` only if commander UI needs it.** The routing agent uses `build_dispatch_payload`. |
| `get_mission_brief` MCP tool | **Don't register** for routing-agent runs. |
| `dispatch_searcher()` ↔ `dispatch_to_cell()` (new) | Both write to `dispatches`. **Keep both.** `dispatch_searcher` for commander UI (assign whole segment + sweep_type), `dispatch_to_cell` for routing agent (single hex). Distinguish at the row level by `segment_id IS NULL`. |
| `get_uncovered_areas()` (segment ranking) ↔ "largest unsearched cluster in view" (new) | Different grain (segment vs. cell flood-fill). **Build the new cluster function inside `build_dispatch_payload`**; don't expose as a tool. |
| `query_route()` (lat/lon → snapped waypoints) ↔ app-side `/field/me/route` | App polls `/field/me/route` for the rendered path; `query_route` is a duplicate skill the LLM doesn't need. **Don't register for routing agent.** |

## Gaps (what needs building)

### 1. `dispatch_to_cell()` write skill

New function in `agent/skills/write.py`:

```python
def dispatch_to_cell(
    user_id: int,
    target_hex_id: int,
    reasoning: str,
    instruction: str | None = None,
    mission_id: int | None = None,
) -> dict:
    """Cell-grain dispatch. Inserts a dispatch row with segment_id=NULL,
    entry_lat/lon set to the target hex's centroid."""
```

- Reuses the existing `dispatches` table (no migration; `segment_id` is nullable, `entry_lat/lon` exist).
- Looks up the target hex's centroid via `SELECT X(Centroid(geom)), Y(Centroid(geom)) FROM hex_cells WHERE id = ?`.
- Supersedes any active dispatch for this user (reuse `_active_dispatches` + `_supersede_dispatches` from `write.py`).
- Sets `user.status = 'dispatched'`.
- Default instruction if none provided: `"Move to hex {target_hex_id}"` (the LLM's reasoning provides the human-readable why).

### 2. `build_dispatch_payload(mission_id, user_id) -> str`

New file `agent/payload.py`. The load-bearing function per the spec. Computes:

| Field | Existing helper to lean on | New work |
|---|---|---|
| 10×10 crop | `api/db/hex_cells.py` has `hex_cell_id_at(mission_id, lat, lon)`; need axial-coord neighborhood traversal | **Need:** function to enumerate the 100 hex IDs around a center hex |
| Symbol per cell | Schema fields all exist (`flag_searched`, `flag_clue`, `flag_impassable`, `is_water`, `is_building`) | **Trivial** — direct mapping per spec |
| PLS bearing/distance | `missions.pls_lat`, `pls_lon`; haversine in `read.py:_haversine_m` | **Trivial** — reuse `_haversine_m`, add bearing function |
| Nearest clue | `get_findings(kind='clue')` exists | Pick nearest by haversine + format staleness from `flags_updated_ts` |
| Largest unsearched cluster | None | **Need:** flood-fill on the 10×10 view |
| Impassable description | None | **Need:** simple bounding-box describer ("rows 9, cols 5–7") |
| Other volunteers in view | `list_searchers()` exists | Filter to those whose latest ping falls inside view bbox |

### 3. The view-anchor wrapper

The model **never sees absolute coords**. It calls `dispatch(col, row, reason)` in local space. Something has to translate (col, row) + anchor → world hex_id. Two options:

**A. Closure-bound tool per LLM invocation** (recommended)
```python
def make_dispatch_tool(mission_id: int, user_id: int, view_anchor: dict):
    def dispatch(target_col: int, target_row: int, reasoning: str):
        hex_id = anchor_to_world_hex(view_anchor, target_col, target_row)
        return dispatch_to_cell(user_id, hex_id, reasoning, mission_id=mission_id)
    return dispatch
```
The wrapper registers this closure as the LLM's `dispatch` tool for that single invocation. Clean, no schema impact.

**B. Side-channel "current view" state in DB** (avoid)
Storing the anchor in a `dispatch_sessions` table. Adds state to keep consistent across calls. Don't do this for the hackathon.

### 4. `workers/agent.py` — the cron worker

Doesn't exist. Needs:
- Iterate `read.active_missions()`.
- For each mission, iterate `list_searchers()` and pick those needing dispatch (no active dispatch, or stale target).
- For each: build payload, invoke nemoclaw/openclaw with the system prompt from the spec + the user message from `build_dispatch_payload()`, register the closure-bound `dispatch` tool, run one turn.
- Cron entry per `2026-05-15-sar-mission-control-design.md:457`: `* * * * * cd /home/asus/geo-beacon && .venv/bin/python -m workers.agent`.

### 5. MCP server tool registration for routing agent

Either:
- Add a flag/env var so `mcp_server.py` registers only the routing-agent tool set (`dispatch_to_cell` + maybe `get_searcher` for debugging), or
- Build the routing agent **without going through MCP** — it's a one-shot Python script that calls the LLM directly and invokes `dispatch_to_cell` in-process. The spec doesn't require MCP for this path; MCP made sense for the commander-grade brief loop, less so for a tight 200-token dispatch loop.

I'd skip MCP for routing-agent. Direct in-process call is simpler, faster, easier to debug.

## What's reusable from current code

Listed for the agent person so they don't rebuild:

- `agent.skills.read._haversine_m` — distance helper for facts
- `agent.skills.read._resolve_mission_id` — same pattern; reuse or copy
- `agent.skills.read.list_searchers()` — gives per-user latest pings + dispatches in one query, perfect for "other volunteers in view"
- `agent.skills.write._require_reason` + the `BEGIN/COMMIT` transaction pattern — copy for `dispatch_to_cell`
- `agent.skills.write._active_dispatches` + `_supersede_dispatches` — needed by `dispatch_to_cell` to avoid duplicate active dispatches per user
- `api/db/hex_cells.py:hex_cell_id_at(mission_id, lat, lon)` — point-in-polygon helper for translating volunteer's GPS to their containing hex
- `api/db/geojson.py` patterns for `AsGeoJSON(geom)` — only relevant if the agent ever needs to return geometry; the dispatch tool returns just an id, so probably not

## Schema status (what was thought to be blocking but isn't)

The dispatch-agent doc flagged Eric's `hex_cells.searched` column as a blocker. **It's already done.**

- `hex_cells.flag_searched INTEGER` — migration `004_searched_flag.sql`, default 0.
- Maintained by `api/db/hex_cells.py:mark_hex_searched` (first-writer-wins).
- Wired in via ping ingestion path — see commit `b2721dd "Coverage attribution is now first-writer-wins"`.
- Also have `searched_by_user_id`, `searched_ts` for per-volunteer coverage attribution.

So the pre-compute can read `flag_searched` directly. The 30-min-track fallback the spec suggested isn't needed.

`hex_cells.flag_clue`, `flags_updated_ts` also exist. `findings.hex_id` joins findings to hexes (`api/db/findings.py` per the schema in `read.py:get_findings`).

## Recommended integration plan

In dependency order:

1. **Verify ping → `mark_hex_searched` wiring** (one query: `SELECT COUNT(*) FROM hex_cells WHERE flag_searched = 1 AND mission_id = <test>` after the simulator runs). If zero, find the gap. Should be fine based on `b2721dd`.
2. **Write `dispatch_to_cell()` in `agent/skills/write.py`** — copies the transaction skeleton from `dispatch_searcher`, swaps segment lookup for hex lookup. ~50 lines.
3. **Write `build_dispatch_payload(mission_id, user_id)` in `agent/payload.py`** — the bulk of new work. Standalone, runnable from the CLI for eyeball-debugging before any LLM is involved.
4. **Write a tiny `workers/agent.py`** that picks one volunteer, builds the payload, calls nemoclaw with the system prompt, and invokes the closure-bound `dispatch` tool. Single-volunteer first; loop over volunteers later.
5. **Verify end-to-end on the seeded mission** with one phone. The `dispatches` row should land with `segment_id=NULL` and `entry_lat/lon` populated.
6. **App-side** (if not already): confirm `/field/me` returns a dispatch with `segment_id=NULL` and the app renders the pin at `entry_lat/lon`. The `recall_searcher` flow already produces this shape, so this should be working.
7. **Wire the cron** per the spec (60 s tick).

## Decisions for the team

- **Delete the segment-grain commander tools?** Yes. Mission creation, hazard flagging, broadcasts, POA updates, recalls, reassigns — none have a remaining caller. `dispatch_searcher` is borderline (you might want curl-able segment dispatch for hand-debugging the app); `dispatch_to_cell` is the only path that matters for the demo. Recommend: delete everything except `dispatch_to_cell` + a thin read path for debugging.
- **Delete `agent/brief.py` + `get_mission_brief`?** Yes — confirmed dead. Brief-creation pipeline is deprecated.
- **Delete `agent/mcp_server.py`?** Probably. Routing agent runs in-process (see next bullet). MCP made sense for the commander brief loop; with that gone, MCP carries no traffic. Strip to a debug subset or remove entirely.
- **MCP vs direct in-process for the routing agent?** Direct. The dispatch loop is too tight to justify the MCP round-trip, and the closure-bound tool pattern from §3 above doesn't fit MCP cleanly (MCP tools are static, not per-invocation).
- **Do we even need the LLM here?** Same question raised in the dispatch-agent doc. The pre-compute is doing 80% of the work; a heuristic (`max(unsearched_cells, key=score)`) gets you most of the way. Ship the LLM version, watch what it picks for 5 minutes, decide whether to keep it.

## What NOT to build

For the same reason the dispatch-agent doc dropped narrative framing:

- **Don't add new read tools for "context" the routing agent doesn't use.** PLS, nearest clue, unsearched-cluster, hazards-in-view, other-volunteers-in-view are all inside `build_dispatch_payload`. They are not separate tool calls — they're pre-computed Python that ends up as text in the user message. Resist the urge to make each one a tool.
- **Don't expose `update_segment_poa`, `flag_hazard`, `broadcast`, `recall_searcher`, `reassign_searcher` to the routing agent.** They're for the commander tier. The routing agent's only verb is `dispatch`.
- **Don't add a `dispatch_session` or `view_state` table.** The closure pattern handles per-invocation state in process memory. No schema, no cleanup.
