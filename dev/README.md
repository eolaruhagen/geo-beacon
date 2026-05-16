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
- A Python interpreter that supports sqlite **extension loading** — Apple's
  `/usr/bin/python3` does NOT. Any of these work:
  - Homebrew: `brew install python@3.12`
  - Conda: any env you create yourself (`conda create -n geo python=3.12`)
  - Pyenv: `PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions" pyenv install 3.12.5`
- Homebrew (for `libspatialite` + `ngrok`)

## One-time setup

```bash
# Default — uses whatever python3 is on PATH:
./dev/setup.sh

# Or point at a specific interpreter (recommended on macOS, since the default
# python3 is Apple's and can't load mod_spatialite):
PYTHON=python3.12 ./dev/setup.sh
PYTHON=$(which python) ./dev/setup.sh        # if a conda env is active
PYTHON=/opt/homebrew/bin/python3.12 ./dev/setup.sh
```

What it does:

1. `brew install libspatialite ngrok` (idempotent)
2. Validates that `$PYTHON` can load sqlite extensions — fails fast with
   install hints if not
3. Creates `.venv/` at the repo root using the validated interpreter
4. `pip install -r requirements.txt` into the venv
5. Creates `dev/data/` for the local DB file

If you previously ran `./dev/setup.sh` with the wrong Python, just re-run it
with `PYTHON=...` set — the script detects an extension-incompatible venv and
rebuilds it.

Re-running is otherwise safe — every step is idempotent.

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

## Phone testing

1. Start the server: `./dev/run-api.sh`
2. In another terminal: `ngrok http 8000` (or `./dev/run-ngrok.sh`)
3. Copy the `https://` URL into the phone app's Server URL field.
4. Seed a mission: `python scripts/seed_mission.py` (run from the repo root with the venv active)
5. Share the printed `join_code` with each phone.

ngrok dashboard at http://localhost:4040 shows every request that hit the
tunnel — useful for debugging headers / payload shape without re-running the
phone blindly.

---

## Phone dev — running sar-app on a real iPhone

Two URLs that look the same but serve different things. Keep them straight:

| URL | What it is | Where it goes |
|---|---|---|
| `https://*.exp.direct` | **Metro tunnel** — serves the JS bundle to the dev client | Dev-client launch screen / QR you scan |
| `https://*.ngrok-free.app` | **API tunnel** — serves FastAPI | Mission Selector "Server URL" field in the app |

### First-time / after native changes

Native rebuild is required when you add a native package, change `app.json` plugins, change `Info.plist` keys, or run `prebuild --clean`.

```bash
cd sar-app
npx expo install <new-native-package>      # if adding a package
npx expo prebuild -p ios --clean           # if app.json native config changed
npx expo run:ios --device                  # builds, installs, starts Metro
```

### Daily JS-only loop

```bash
cd sar-app
npx expo start --dev-client --tunnel       # tunnel = required on eduroam / any wifi with client isolation
```

`--tunnel` needs `@expo/ngrok`. If Metro complains about it, install it **as a project dependency** (Expo CLI doesn't reliably pick up the global install on macOS):

```bash
npm install --save-dev @expo/ngrok
```

After `Tunnel ready.` prints in the terminal, **scan the QR with the iPhone Camera app**, not from inside the app. Don't tap the home-screen icon to launch — it'll try the cached LAN URL and fail.

### Wiring the phone to the API

1. Metro tunnel terminal (`expo start --dev-client --tunnel`) — leave running.
2. API terminal: `./dev/run-api.sh`
3. API tunnel terminal: `ngrok http 8000` — copy the `https://*.ngrok-free.app` URL.
4. Seed a mission: `python scripts/seed_mission.py` — note the `join_code`.
5. On the phone: app opens to Mission Selector. Paste the **ngrok API URL** into the Server URL field. Enter display name + join code. Tap Join.

### Common failure modes

**`No script URL provided — unsanitizedScriptURLString = (null)`**
The dev-client launched without a bundler URL. You tapped the icon instead of scanning the QR, or the cached URL is stale. Force-close the app, scan the QR via Camera.

**`The resource could not be loaded because the App Transport Security policy requires the use of a secure connection.`**
The dev-client is trying `http://<lan-ip>:8081` (insecure HTTP + unreachable LAN). You're not on the tunnel — scanned the wrong QR or hit a cached URL. Stop Metro, restart with `--tunnel --clear`, scan the fresh QR.

**Multiple Metro instances running**
The dev-client may pick up the wrong one. Kill all of them (`pkill -f "expo start"`) before restarting.

**ngrok dashboard shows `HEAD /` / `GET /` returning 404**
Not an error. FastAPI has no route at `/`. Real endpoints (`/missions/join`, `/field/ping`, etc.) return 2xx.

**Eduroam / coffeeshop wifi blocks Metro entirely**
Most public/institutional wifi networks have client isolation — phones can't reach laptops on the same SSID. `--tunnel` bypasses this. If the tunnel is also flaky, fall back to a phone-hotspot SSID (Mac joins phone's hotspot, both devices share that private network).

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
