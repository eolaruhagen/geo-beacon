#!/usr/bin/env bash
set -euo pipefail

sandbox_name="${1:-my-assistant}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mcp_url="${GEO_BEACON_MCP_URL:-http://172.17.0.1:8765/mcp}"
server_name="${GEO_BEACON_MCP_SERVER_NAME:-geo-beacon-sar}"

container_id="$(docker ps -aq --filter "name=openshell-${sandbox_name}-" | head -n 1)"
if [[ -z "$container_id" ]]; then
  echo "No OpenClaw container found for sandbox '$sandbox_name'." >&2
  exit 1
fi

docker start "$container_id" >/dev/null
docker exec "$container_id" mkdir -p /sandbox/.openclaw/workspace
docker cp "$repo_root/openclaw/SOUL.md" "$container_id":/sandbox/.openclaw/workspace/SOUL.md
docker cp "$repo_root/openclaw/TOOLS.md" "$container_id":/sandbox/.openclaw/workspace/TOOLS.md
docker cp "$repo_root/AGENTS.md" "$container_id":/sandbox/.openclaw/workspace/AGENTS.md

server_json="{\"url\":\"$mcp_url\",\"transport\":\"streamable-http\"}"
docker exec "$container_id" sh -lc "HOME=/sandbox openclaw mcp set '$server_name' '$server_json'"
docker exec "$container_id" sh -lc "HOME=/sandbox openclaw mcp show '$server_name' --json"

cat <<EOF
OpenClaw workspace installed for sandbox '$sandbox_name'.

Start the host MCP server separately:
  cd "$repo_root"
  ./scripts/run_agent_mcp_http.sh

Configured OpenClaw MCP URL:
  $mcp_url

Routing worker OpenClaw command:
  OPENCLAW_ROUTER_COMMAND=$repo_root/scripts/run_openclaw_router.sh
EOF
