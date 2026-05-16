#!/usr/bin/env bash
# One-time local dev setup: macOS deps, Python venv, pip install.
# Idempotent — safe to re-run after pulling new deps.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
VENV_DIR="$REPO_ROOT/.venv"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1" >&2; }

bold "1/5  Checking macOS / Homebrew"
if [[ "$(uname)" != "Darwin" ]]; then
  warn "Not macOS — brew steps will be skipped. Install libspatialite + ngrok via your package manager."
  SKIP_BREW=1
elif ! command -v brew >/dev/null 2>&1; then
  fail "Homebrew not found. Install from https://brew.sh first."
  exit 1
else
  ok "brew at $(which brew)"
  SKIP_BREW=0
fi

bold "2/5  Installing system deps (libspatialite, ngrok)"
if [[ "$SKIP_BREW" == "0" ]]; then
  for pkg in libspatialite ngrok; do
    if brew list --formula 2>/dev/null | grep -qx "$pkg" \
       || brew list --cask 2>/dev/null | grep -qx "$pkg"; then
      ok "$pkg already installed"
    else
      echo "  installing $pkg ..."
      brew install "$pkg"
      ok "$pkg installed"
    fi
  done
fi

bold "3/5  Python venv at $VENV_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
  ok "created venv"
else
  ok "venv already exists"
fi

bold "4/5  pip install -r requirements.txt"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
ok "deps installed in venv"
deactivate

bold "5/5  Local dev directories"
mkdir -p dev/data dev/seeds
ok "dev/data and dev/seeds present"

echo
bold "Setup complete."
echo
echo "Next steps:"
echo "  1. If ngrok isn't authed yet:  ngrok config add-authtoken <your-token>"
echo "     Token: https://dashboard.ngrok.com/get-started/your-authtoken"
echo "  2. Initialize the DB:           ./dev/reset-db.sh"
echo "  3. Run the API:                 ./dev/run-api.sh"
echo "  4. Phone tunnel:                ./dev/run-ngrok.sh"
echo
echo "Full docs:  dev/README.md"
