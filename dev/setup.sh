#!/usr/bin/env bash
# One-time local dev setup: macOS deps, Python venv, pip install.
# Idempotent — safe to re-run after pulling new deps.
#
# Picks the Python to bootstrap the venv from $PYTHON (default: python3).
# The chosen interpreter MUST support sqlite extension loading
# (Apple's /usr/bin/python3 does not). Examples:
#
#   PYTHON=$(which python3.12) ./dev/setup.sh         # explicit path
#   PYTHON=python3.12          ./dev/setup.sh         # on PATH
#   conda activate myenv && PYTHON=$(which python) ./dev/setup.sh
#   PYTHON=/opt/homebrew/bin/python3.12 ./dev/setup.sh
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
VENV_DIR="$REPO_ROOT/.venv"
PYTHON="${PYTHON:-python3}"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1" >&2; }

check_python() {
  local py="$1"
  if ! command -v "$py" >/dev/null 2>&1 && [[ ! -x "$py" ]]; then
    fail "Python interpreter '$py' not found on PATH or as an executable path."
    cat >&2 <<EOF

  Set \$PYTHON to a Python that exists. Some quick-start options:
    brew install python@3.12         then  PYTHON=python3.12 ./dev/setup.sh
    conda env: activate it, then     PYTHON=\$(which python) ./dev/setup.sh
    pyenv:    PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions" \\
              pyenv install 3.12.5   then  PYTHON=\$(pyenv which python) ./dev/setup.sh
EOF
    exit 1
  fi
  if ! "$py" -c "import sqlite3, sys; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); sys.exit(0)" 2>/dev/null; then
    fail "$py does NOT support sqlite extension loading."
    cat >&2 <<EOF

  This Python was compiled without --enable-loadable-sqlite-extensions,
  so it cannot load mod_spatialite. Apple's /usr/bin/python3 is the
  usual culprit on macOS. Pick a different interpreter:

    brew install python@3.12         then  PYTHON=python3.12 ./dev/setup.sh
    conda:                                 PYTHON=\$(which python) ./dev/setup.sh
    pyenv:    PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions" \\
              pyenv install 3.12.5   then  PYTHON=\$(pyenv which python) ./dev/setup.sh
EOF
    exit 1
  fi
}

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
echo "  using interpreter: $PYTHON (override with PYTHON=...)"
check_python "$PYTHON"
PY_VERSION="$("$PYTHON" -c 'import sys; print("{}.{}.{}".format(*sys.version_info[:3]))')"
ok "$PYTHON v$PY_VERSION supports extension loading"
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON" -m venv "$VENV_DIR"
  ok "created venv"
else
  # Detect mismatched venv (built with a different interpreter that may lack ext loading)
  VENV_PY="$VENV_DIR/bin/python"
  if ! "$VENV_PY" -c "import sqlite3; sqlite3.connect(':memory:').enable_load_extension(True)" 2>/dev/null; then
    warn "existing venv was built with a Python that can't load sqlite extensions"
    warn "rebuilding venv from $PYTHON"
    rm -rf "$VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
    ok "venv rebuilt"
  else
    ok "venv already exists (extension loading OK)"
  fi
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
