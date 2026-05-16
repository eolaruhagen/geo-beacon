#!/usr/bin/env bash
# Run FastAPI locally with auto-reload.
# Env overrides:
#   PORT            (default 8000)
#   HOST            (default 0.0.0.0)
#   LOG_LEVEL       (default info; try 'debug' for verbose)
#   MISSION_DB_PATH (default dev/data/mission.db)
#   APP_MODULE      (default api.main:app)
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
VENV_DIR="$REPO_ROOT/.venv"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
LOG_LEVEL="${LOG_LEVEL:-info}"
APP_MODULE="${APP_MODULE:-api.main:app}"
export MISSION_DB_PATH="${MISSION_DB_PATH:-$REPO_ROOT/dev/data/mission.db}"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1" >&2; }

if [[ ! -d "$VENV_DIR" ]]; then
  fail "venv missing. Run ./dev/setup.sh first."
  exit 1
fi

# Helpful early error rather than a confusing uvicorn import trace.
MODULE_FILE="${APP_MODULE%%:*}"
MODULE_FILE="${MODULE_FILE//.//}.py"
if [[ ! -f "$MODULE_FILE" ]]; then
  fail "$MODULE_FILE doesn't exist — nobody has scaffolded the FastAPI app yet."
  echo "  Override the module via APP_MODULE if it lives somewhere else." >&2
  echo "  Example: APP_MODULE=api.skeleton:app ./dev/run-api.sh" >&2
  exit 1
fi

ok "MISSION_DB_PATH=$MISSION_DB_PATH"
ok "uvicorn $APP_MODULE on http://$HOST:$PORT"
echo

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
RELOAD_ARGS=(--reload)
for d in api workers agent; do
  [[ -d "$REPO_ROOT/$d" ]] && RELOAD_ARGS+=(--reload-dir "$REPO_ROOT/$d")
done

exec uvicorn "$APP_MODULE" \
  --host "$HOST" \
  --port "$PORT" \
  --log-level "$LOG_LEVEL" \
  "${RELOAD_ARGS[@]}"
