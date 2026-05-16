#!/usr/bin/env bash
# Start the per-searcher routing agent in a background loop.
# Defaults to LLM mode using scripts/openclaw_invoke.py as the bridge to
# OpenClaw inside the docker sandbox.
#
# Usage:
#   ./scripts/run_router_agent.sh                    # llm mode, 30s loop
#   AGENT_MODE=heuristic ./scripts/run_router_agent.sh
#   AGENT_INTERVAL=60 ./scripts/run_router_agent.sh
#   AGENT_MISSION_ID=2 ./scripts/run_router_agent.sh
#
# Tail the log: tail -f logs/agent.log
# Kill the loop: kill $(cat logs/agent.pid)
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
VENV="$REPO_ROOT/.venv"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"

MODE="${AGENT_MODE:-llm}"
INTERVAL="${AGENT_INTERVAL:-30}"
MISSION_ARG=""
if [[ -n "${AGENT_MISSION_ID:-}" ]]; then
  MISSION_ARG="--mission-id $AGENT_MISSION_ID"
fi

if [[ "$MODE" == "llm" ]]; then
  export OPENCLAW_ROUTER_COMMAND="${OPENCLAW_ROUTER_COMMAND:-$REPO_ROOT/scripts/openclaw_invoke.py}"
  chmod +x "$REPO_ROOT/scripts/openclaw_invoke.py"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "router agent: mode=$MODE interval=${INTERVAL}s mission=${AGENT_MISSION_ID:-all}"
echo "log: $LOG_DIR/agent.log     pid: $LOG_DIR/agent.pid"

nohup python -m workers.agent \
  $MISSION_ARG \
  --mode "$MODE" \
  --loop --interval-seconds "$INTERVAL" \
  --skip-active \
  >> "$LOG_DIR/agent.log" 2>&1 &

echo $! > "$LOG_DIR/agent.pid"
echo "started pid $(cat "$LOG_DIR/agent.pid")"
