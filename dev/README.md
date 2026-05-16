# Local dev harness

Run FastAPI + SQLite + SpatiaLite on your Mac for fast API iteration. No DGX, no
agent loop, no openclaw. Just the API surface and the DB. Phone reaches it via
an ngrok tunnel so you can test on real cellular.

This harness is **schema-agnostic** — it doesn't know about specific tables or
migrations. It just runs whatever's in `../migrations/` against a local DB file
and applies any SQL it finds in `seeds/`.

---

## TL;DR

```bash
./dev/setup.sh         # one-time: brew deps + venv + pip install
./dev/reset-db.sh      # creates dev/data/mission.db, applies migrations + seeds
./dev/run-api.sh       # uvicorn on :8000 (reload on file change)

# in a second terminal, when you want phone access:
./dev/run-ngrok.sh     # prints the https URL to paste into the Shortcut / app
```

That's the whole loop. Edit code → uvicorn reloads → hit endpoints from your
phone via the ngrok URL → inspect `dev/data/mission.db` with `sqlite3`.

---

## Prerequisites

- macOS (Apple Silicon or Intel) — Linux works too with apt-equivalent steps
- Python 3.12+
- Homebrew

## One-time setup

```bash
./dev/setup.sh
```

What it does:

1. `brew install libspatialite ngrok` (idempotent)
2. Creates `.venv/` at the repo root
3. `pip install -r requirements.txt` into the venv
4. Creates `dev/data/` for the local DB file
5. Prompts you to configure `ngrok` with your authtoken if you haven't already

Re-running is safe — every step is idempotent.

## Daily workflow

### Start the API

```bash
./dev/run-api.sh
```

Defaults:
- `MISSION_DB_PATH=<repo>/dev/data/mission.db`
- Host: `0.0.0.0` (so other devices on your LAN can reach it directly)
- Port: `8000`
- `--reload` is on, so saving a `.py` file restarts the worker

Override env vars inline if you need to:

```bash
PORT=9000 LOG_LEVEL=debug ./dev/run-api.sh
```

### Expose to your phone

```bash
./dev/run-ngrok.sh
```

This prints a `https://<random>.ngrok-free.app` URL. Set it in your Shortcut /
app config. The URL changes every time ngrok restarts on the free tier.

### Reset the DB

When migrations change or seeds change and you want a clean slate:

```bash
./dev/reset-db.sh
```

What it does:

1. Deletes `dev/data/mission.db*` (DB + WAL + SHM if present)
2. Re-runs `scripts/apply_migrations.py` against a fresh file
3. Runs `dev/seed.sh` to apply any SQL files in `dev/seeds/`

Add `--keep-data` to skip the delete (just re-apply pending migrations + seeds
without wiping rows):

```bash
./dev/reset-db.sh --keep-data
```

### Seed data

Drop `.sql` files into `dev/seeds/`. They get applied in lexical order after
migrations. Examples (once your schema stabilizes):

- `dev/seeds/01_users.sql` — create a few test searchers with known bearer tokens
- `dev/seeds/02_mission.sql` — create a test mission with a small bbox
- `dev/seeds/03_segments.sql` — pre-populate segments for that mission

Seeds are **not** tracked in `schema_migrations`. Re-applying them is the
caller's responsibility (use `INSERT OR REPLACE`, `ON CONFLICT DO NOTHING`, or
just `./dev/reset-db.sh` for full rebuild).

### Inspect the DB

```bash
sqlite3 dev/data/mission.db
sqlite> .load mod_spatialite
sqlite> .tables
sqlite> SELECT * FROM pings ORDER BY ts DESC LIMIT 5;
```

Or use a GUI like [DB Browser for SQLite](https://sqlitebrowser.org/) — but
spatial functions need the `mod_spatialite` extension loaded first, which
DB Browser supports via Preferences → SQL extension to load.

---

## Phone testing recipe

1. `./dev/run-api.sh` in terminal 1
2. `./dev/run-ngrok.sh` in terminal 2
3. Copy the `https://...ngrok-free.app` URL
4. On your iPhone, edit your Shortcut to POST to that URL + path (e.g.
   `https://abc-xyz.ngrok-free.app/field/ping`) with your test payload
5. Run the Shortcut, watch the uvicorn terminal for the request log
6. `sqlite3 dev/data/mission.db "SELECT * FROM pings ORDER BY ts DESC LIMIT 1"`
   to confirm the row landed

If the request never reaches uvicorn, check: ngrok dashboard at
http://localhost:4040 shows every request that hit the tunnel — useful for
debugging headers / payload shape without re-running the Shortcut blindly.

---

## Troubleshooting

**`ImportError: No module named 'api'` or `ModuleNotFoundError: api`**
You're not in the venv or running uvicorn from the wrong cwd. Always invoke via
`./dev/run-api.sh`, which sets both correctly.

**`OperationalError: not authorized` when loading spatialite**
Means `enable_load_extension(True)` was skipped. `api/db.py` and
`scripts/apply_migrations.py` both do this; if you wrote your own connection,
add it.

**`Could not load mod_spatialite`**
The dylib isn't where the lookup expects. On Apple Silicon it lives at
`/opt/homebrew/lib/mod_spatialite.dylib` after `brew install libspatialite`.
Set `SPATIALITE_PATH` to the full path if auto-discovery fails:

```bash
export SPATIALITE_PATH="/opt/homebrew/lib/mod_spatialite.dylib"
```

**`Address already in use` on :8000**
Something else is on port 8000. Find it: `lsof -i :8000`. Either kill it or
run with `PORT=8001 ./dev/run-api.sh`.

**ngrok prompts for authtoken on every run**
You haven't configured it. Run `ngrok config add-authtoken <your-token>` once
(get the token at https://dashboard.ngrok.com/get-started/your-authtoken).

**`api/main.py` doesn't exist yet**
This harness doesn't ship one — `run-api.sh` will error until somebody scaffolds
the FastAPI app. The harness itself is ready; the API code is downstream of the
schema settling.

---

## File layout

```
dev/
├── README.md           # this file
├── setup.sh            # one-time deps + venv
├── reset-db.sh         # wipe + migrate + seed
├── seed.sh             # apply dev/seeds/*.sql
├── run-api.sh          # uvicorn launcher
├── run-ngrok.sh        # ngrok launcher
├── seeds/              # local-only seed SQL (you author these)
│   └── README.md
└── data/               # local DB lives here (gitignored)
    └── mission.db      # created by reset-db.sh
```
