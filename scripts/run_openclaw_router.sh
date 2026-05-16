#!/usr/bin/env bash
set -euo pipefail

# Host-side stdin runner for workers/agent.py.
# Reads one routing prompt from stdin, sends it into the OpenClaw sandbox, and
# prints OpenClaw's JSON response to stdout.

sandbox_name="${OPENCLAW_SANDBOX_NAME:-my-assistant}"
container_id="${OPENCLAW_CONTAINER_ID:-}"

if [[ -z "$container_id" ]]; then
  container_id="$(docker ps -aq --filter "name=openshell-${sandbox_name}-" | head -n 1)"
fi
if [[ -z "$container_id" ]]; then
  echo "No OpenClaw container found for sandbox '$sandbox_name'." >&2
  exit 1
fi

prompt="$(cat)"
session_id="${GB_OPENCLAW_SESSION_ID:-routing-$(date +%s)}"
thinking="${GB_OPENCLAW_THINKING:-off}"
timeout="${GB_OPENCLAW_TIMEOUT:-90}"

docker start "$container_id" >/dev/null
docker exec -i -u sandbox \
  --env HOME=/sandbox \
  --env "ROUTING_PROMPT=$prompt" \
  --env "GB_OPENCLAW_SESSION_ID=$session_id" \
  --env "GB_OPENCLAW_THINKING=$thinking" \
  --env "GB_OPENCLAW_TIMEOUT=$timeout" \
  "$container_id" sh -lc '
    . /tmp/nemoclaw-proxy-env.sh >/dev/null 2>&1 || true
    export HOME=/sandbox
    exec openclaw agent \
      --agent main \
      --session-id "$GB_OPENCLAW_SESSION_ID" \
      --thinking "$GB_OPENCLAW_THINKING" \
      --timeout "$GB_OPENCLAW_TIMEOUT" \
      --json \
      --message "$ROUTING_PROMPT"
  '
