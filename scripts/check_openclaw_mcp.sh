#!/usr/bin/env bash
# Verify that OpenClaw inside the sandbox sees the geo-beacon-sar MCP server.
#
# Usage:
#   ./scripts/check_openclaw_mcp.sh                 # uses sandbox name 'my-assistant'
#   ./scripts/check_openclaw_mcp.sh other-name      # override sandbox name
#
# Exits 0 if registered and reachable, non-zero otherwise.
set -euo pipefail

SANDBOX="${1:-${GEO_BEACON_SANDBOX:-my-assistant}}"
CID="$(docker ps -qf "name=openshell-${SANDBOX}-" | head -1)"

if [[ -z "$CID" ]]; then
  echo "FAIL: no running container matching 'openshell-${SANDBOX}-'" >&2
  echo "      try: docker ps -a" >&2
  exit 2
fi

echo "container: $CID"
echo
echo "--- openclaw mcp list ---"
docker exec -u sandbox "$CID" sh -lc 'HOME=/sandbox openclaw mcp list'
echo
echo "--- openclaw mcp show geo-beacon-sar ---"
docker exec -u sandbox "$CID" sh -lc 'HOME=/sandbox openclaw mcp show geo-beacon-sar --json' || {
  echo "FAIL: geo-beacon-sar not registered. Run ./scripts/install_openclaw_workspace.sh" >&2
  exit 3
}
