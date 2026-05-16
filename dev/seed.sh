#!/usr/bin/env bash
# Apply every *.sql file in dev/seeds/ to the local DB in lexical order.
# Loads mod_spatialite first so seeds can use spatial functions.
# Seeds are not tracked — re-applying is the caller's responsibility (use
# INSERT OR REPLACE / ON CONFLICT DO NOTHING, or just run reset-db.sh).
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
VENV_DIR="$REPO_ROOT/.venv"
DB_PATH="${MISSION_DB_PATH:-$REPO_ROOT/dev/data/mission.db}"
SEEDS_DIR="$REPO_ROOT/dev/seeds"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1" >&2; }

if [[ ! -f "$DB_PATH" ]]; then
  fail "DB not found at $DB_PATH — run ./dev/reset-db.sh first."
  exit 1
fi

shopt -s nullglob
SEED_FILES=("$SEEDS_DIR"/*.sql)

if [[ ${#SEED_FILES[@]} -eq 0 ]]; then
  warn "No seed files in $SEEDS_DIR (this is fine — schema may not be finalized yet)."
  exit 0
fi

# Resolve mod_spatialite location for sqlite3 CLI .load directive.
# Python's load_extension is more forgiving; the sqlite3 CLI wants an explicit path.
SPATIALITE_LIB="${SPATIALITE_PATH:-}"
if [[ -z "$SPATIALITE_LIB" ]]; then
  for candidate in \
    /opt/homebrew/lib/mod_spatialite.dylib \
    /usr/local/lib/mod_spatialite.dylib \
    /usr/lib/aarch64-linux-gnu/mod_spatialite.so \
    /usr/lib/x86_64-linux-gnu/mod_spatialite.so; do
    if [[ -e "$candidate" ]]; then
      SPATIALITE_LIB="$candidate"
      break
    fi
  done
fi

if [[ -z "$SPATIALITE_LIB" ]]; then
  fail "Could not locate mod_spatialite. Set SPATIALITE_PATH to its full path."
  exit 1
fi

for seed in "${SEED_FILES[@]}"; do
  echo "  applying $(basename "$seed") ..."
  sqlite3 "$DB_PATH" <<SQL
.load $SPATIALITE_LIB
.read $seed
SQL
  ok "$(basename "$seed")"
done

ok "all seeds applied"
