#!/usr/bin/env bash
# Reset the local dev DB: wipe + migrate + seed.
# Schema-agnostic — runs whatever migrations and seeds happen to exist.
#
# Usage:
#   ./dev/reset-db.sh              # wipe and rebuild (destructive)
#   ./dev/reset-db.sh --keep-data  # apply pending migrations + seeds without wiping
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
VENV_DIR="$REPO_ROOT/.venv"
DB_PATH="${MISSION_DB_PATH:-$REPO_ROOT/dev/data/mission.db}"
DB_DIR="$(dirname "$DB_PATH")"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }

KEEP_DATA=0
for arg in "$@"; do
  case "$arg" in
    --keep-data) KEEP_DATA=1 ;;
    -h|--help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

if [[ ! -d "$VENV_DIR" ]]; then
  warn "venv missing — running ./dev/setup.sh first"
  ./dev/setup.sh
fi

mkdir -p "$DB_DIR"

if [[ "$KEEP_DATA" == "0" ]]; then
  bold "1/3  Wiping $DB_PATH (and -wal / -shm if present)"
  rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"
  ok "DB files removed"
else
  bold "1/3  Keeping existing data (--keep-data)"
  if [[ ! -f "$DB_PATH" ]]; then
    warn "No existing DB found; effectively a clean rebuild."
  fi
fi

bold "2/3  Applying migrations from migrations/"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
MISSION_DB_PATH="$DB_PATH" python scripts/apply_migrations.py
ok "migrations applied"

bold "3/3  Applying seeds from dev/seeds/*.sql"
./dev/seed.sh
deactivate

echo
ok "Local DB ready at $DB_PATH"
echo
echo "Inspect with:"
echo "  sqlite3 $DB_PATH"
echo "  sqlite> .load mod_spatialite"
echo "  sqlite> .tables"
