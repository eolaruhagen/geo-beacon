# Geo-Beacon Tools

Geo-Beacon exposes one MCP server named `geo-beacon-sar`.

The MCP server runs on the DGX host, not inside the OpenClaw sandbox. This is
intentional: the host process can read and write the live SQLite/SpatiaLite
mission database while OpenClaw reaches it over local streamable HTTP.

## MCP Server

- Name: `geo-beacon-sar`
- URL from OpenClaw sandbox: `http://172.17.0.1:8765/mcp`
- Host start command: `./scripts/run_agent_mcp_http.sh`
- Database env: `MISSION_DB_PATH=/home/asus/sqlite/mission.db`

## Read Tools

- `get_mission_brief`
- `get_mission_overview`
- `get_uncovered_areas`
- `get_segment`
- `get_searcher`
- `get_findings`
- `get_terrain_summary`
- `query_route`
- `recent_events`

## Write Tools

- `dispatch_searcher`
- `reassign_searcher`
- `recall_searcher`
- `broadcast`
- `flag_hazard`
- `update_segment_poa`
- `update_mission_status`

## Usage Pattern

1. Read `get_mission_brief`.
2. Use read tools to verify any missing detail.
3. Use write tools only for concrete mission actions.
4. Keep every `instruction` short enough for a field phone.
5. Put the evidence for the action in the required `reasoning` argument.
