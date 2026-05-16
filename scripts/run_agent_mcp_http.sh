#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

export MISSION_DB_PATH="${MISSION_DB_PATH:-/home/asus/sqlite/mission.db}"
export GEO_BEACON_MCP_TRANSPORT="${GEO_BEACON_MCP_TRANSPORT:-streamable-http}"
export GEO_BEACON_MCP_HOST="${GEO_BEACON_MCP_HOST:-172.17.0.1}"
export GEO_BEACON_MCP_PORT="${GEO_BEACON_MCP_PORT:-8765}"

local_spatialite="$repo_root/dev/data/spatialite_pkg/root/usr/lib/aarch64-linux-gnu/mod_spatialite.so"
local_spatialite_dir="$(dirname "$local_spatialite")"
if [[ -z "${SPATIALITE_PATH:-}" && -f "$local_spatialite" ]]; then
  export SPATIALITE_PATH="$local_spatialite"
  export LD_LIBRARY_PATH="$local_spatialite_dir:${LD_LIBRARY_PATH:-}"
fi

exec "$repo_root/.venv/bin/python" -m agent.mcp_server
