#!/usr/bin/env bash
# Start an ngrok tunnel pointing at the local API.
# Default port matches dev/run-api.sh (8000). Override with PORT env var.
#
# After it starts, the public URL is printed in ngrok's terminal UI AND is
# queryable at http://localhost:4040/api/tunnels (handy for scripts).
set -euo pipefail

PORT="${PORT:-8000}"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1" >&2; }

if ! command -v ngrok >/dev/null 2>&1; then
  fail "ngrok not installed. Run ./dev/setup.sh (or 'brew install ngrok')."
  exit 1
fi

# Friendly nudge if no authtoken configured yet — ngrok will still error but
# the message is buried in its TUI.
if ! ngrok config check >/dev/null 2>&1; then
  fail "ngrok is not authenticated."
  echo "  Get a token at https://dashboard.ngrok.com/get-started/your-authtoken" >&2
  echo "  Then run: ngrok config add-authtoken <token>" >&2
  exit 1
fi

ok "tunneling http://localhost:$PORT — public URL will appear below"
ok "local dashboard: http://localhost:4040 (shows every inbound request)"
echo

exec ngrok http "$PORT"
