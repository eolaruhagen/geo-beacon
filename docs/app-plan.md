# SAR App — Phase 1 Scope

**Goal:** Get the iPhone app on a physical device, two volunteers can join a mission and see their own GPS positions on a map, and every GPS ping persists to the DB. This is the data-collection foundation. No agent, no flags, no chat yet.

**Success criterion:** Two phones running the app, both pinging GPS, both appearing on each other's map, and `SELECT * FROM pings` on the DGX shows rows arriving in real time.

---

## Stack lock-in (do not deviate)

- **App:** Expo SDK 53, TypeScript, Expo Router
- **Bundle ID:** `app.timeslicer.sarapp`
- **Map library:** `react-native-maps` (Apple Maps base layer on iOS)
- **Location:** `expo-location`
- **Storage:** `expo-secure-store` for bearer token, user_id, mission_id
- **Server:** FastAPI on the DGX (Python)
- **DB:** SQLite + SpatiaLite at `/home/asus/sqlite/mission.db`
- **Network:** HTTP over ngrok tunnel from FastAPI on DGX. App stores the ngrok URL in a constants file.

**Do not:**
- Add navigation libraries beyond Expo Router
- Upgrade Expo SDK
- Add websockets
- Add native push notifications
- Suggest Expo Go

---

## DO NOT GUESS THE SCHEMA

The DB and migrations already exist. Before writing any server code, run:

```bash
sqlite3 /home/asus/sqlite/mission.db ".schema"
```

Read every table definition. Match Python types to actual SQLite column types. If a column is missing that the spec implies should exist, **stop and ask** — do not invent migrations. The schema is canonical.

Specifically: confirm the exact names and types of these columns before writing handlers:
- `users` table — what's the bearer token column called? `bearer_token`? `token`?
- `pings` table — what's the geometry column called? `geom`? `position`? Is it `POINT` or stored as lat/lon floats only?
- `missions` table — what's the area geometry column called?
- `subcells` table — does it exist yet? If yes, what are its columns?

When you find a mismatch between this doc and the actual schema, the schema wins.

---

## What's in scope for Phase 1

### Server (FastAPI on DGX)

**Three endpoints, that's it:**

1. `GET /missions/active`
   - Returns list of `{id, name, subject_description, volunteer_count}` for missions with status='active'
   - No auth required
   - `volunteer_count` is `SELECT COUNT(*) FROM users WHERE current_mission_id = ? AND role = 'searcher'` (or however the schema models it — check first)

2. `POST /missions/{mission_id}/join`
   - Body: `{name: string, zone_geojson: object}` — note `zone_geojson` is accepted but **not stored or used** in Phase 1. Pass through for forward compatibility but don't error on it.
   - Creates a new row in `users` with role='searcher', generates a bearer_token (hex string), assigns to mission
   - Returns: `{user_id: int, callsign: string, bearer_token: string}`
   - Callsign: assign next available from Alpha/Bravo/Charlie/Delta/Echo/Foxtrot
   - No auth required

3. `POST /field/ping`
   - Body: `{lat: float, lon: float, accuracy_m: float, speed_mps?: float, battery_pct?: int}`
   - Requires `X-Bearer-Token` header
   - Looks up user from token. If not found, return 401.
   - Inserts row into `pings` table with current timestamp, user_id, mission_id (from user record), source='phone'
   - Returns: 200 with empty body `{}`

**That's all the endpoints for Phase 1.** No `/mission/state`, no chat, no leave, no findings.

### App (Expo)

**Two screens, that's it:**

#### Screen 1 — Mission Selector

- App title: "SAR" at top
- "Your name" text field, prefilled if previously entered (cached in `expo-secure-store`)
- "Server URL" text field for the ngrok URL (cached). For Phase 1 we just type it in — no QR code, no discovery.
- Pull-to-refresh list of active missions from `GET /missions/active`
- Each row: mission name, subject description (truncated to one line), "X volunteers" count
- Tap a mission → POST to `/missions/{id}/join` with `{name, zone_geojson: {}}` (empty zone for Phase 1)
- On successful join: store bearer_token, user_id, callsign, mission_id in secure-store, navigate to Screen 2
- If no active missions: empty state "No active missions"

#### Screen 2 — Map View

- Top bar:
  - Left: X button → confirms ("Leave mission?"), clears stored token/mission_id, returns to Screen 1. **Phase 1: this just clears local state. No server call.** We'll add `/missions/{id}/leave` in Phase 2.
  - Center: mission name (from cached state)
  - Right: callsign badge
- Map (`react-native-maps`, fills the rest of the screen):
  - User's current location as a blue dot (`showsUserLocation` prop is fine — Apple's built-in)
  - That's it for Phase 1. No subcells, no other volunteers, no overlays.
- Map auto-centers on user's location on first GPS fix, then stays where the user pans it

**Location tracking:**
- Use `expo-location` `watchPositionAsync`
- Foreground accuracy: `Accuracy.BestForNavigation` or `Accuracy.High`
- Ping the server every **5 seconds** with current position
- Use `expo-task-manager` + `Location.startLocationUpdatesAsync` for background updates (the app must keep streaming when screen is locked)
- Background ping interval: 30 seconds

If a ping fails (network error, 401, etc.), log to console and retry on next interval. No retry queue, no offline storage in Phase 1. Real data loss is acceptable in Phase 1; this is just the proving run.

### Configuration

Create `app/config.ts`:
```ts
export const FOREGROUND_PING_INTERVAL_MS = 5000;
export const BACKGROUND_PING_INTERVAL_MS = 30000;
export const DEFAULT_SERVER_URL = ""; // user enters via UI
```

Store the server URL in secure-store under key `serverUrl`. All HTTP calls read from there.

---

## Out of scope for Phase 1

These are explicitly *not* part of this phase. Don't implement them. Don't even write stubs for them.

- Chat system (no `/chat/*` endpoints, no chat button on map)
- Other volunteers' positions on the map
- Subcell grid (no rendering, no traversability, no marked-searched logic)
- Mission selector's "draw a search zone" map step (just join without a zone)
- Flags on subcells
- Agent, agent context, agent invocation queue
- Anything in the agent/ directory of the repo
- Spatial worker
- Replay worker
- Mission dashboard (web)
- Mission creation endpoint (`POST /admin/mission`) — seed missions manually with a script for now
- POD/POA math
- Findings, hazards, broadcasts
- "Leave mission" server endpoint

If the schema has tables for the above, leave them empty. Just don't write code that touches them.

---

## File layout to create

```
sar-app/
├── app.json                          # already exists, ensure bundleId + permissions
├── app/
│   ├── _layout.tsx                   # Expo Router root, decides screen 1 vs 2 based on stored mission_id
│   ├── index.tsx                     # Screen 1 (mission selector)
│   ├── mission.tsx                   # Screen 2 (map view)
│   ├── config.ts                     # constants
│   ├── lib/
│   │   ├── api.ts                    # fetch wrappers for the 3 endpoints
│   │   ├── storage.ts                # secure-store helpers
│   │   └── location.ts               # location streaming + ping loop
```

Server side (in the existing `geo-beacon/` repo on the DGX):

```
geo-beacon/
├── api/
│   ├── main.py                       # FastAPI app
│   ├── db.py                         # SQLite + SpatiaLite connection
│   ├── auth.py                       # bearer token middleware
│   ├── schemas.py                    # pydantic models for the 3 endpoints
│   └── routes/
│       └── field.py                  # all 3 Phase 1 endpoints live here
├── scripts/
│   └── seed_phase1_mission.py        # creates one test mission so the app has something to join
```

`seed_phase1_mission.py`: idempotent script that inserts one mission row with name "UCSC Phase 1 Test", subject_description "Phase 1 test target", status 'active', and reasonable lat/lon for the UCSC area. Run it once on the DGX before testing.

---

## app.json requirements

Verify these exist in `app.json` under `expo.ios.infoPlist`:

```json
{
  "NSLocationWhenInUseUsageDescription": "SAR uses your location to coordinate search areas.",
  "NSLocationAlwaysAndWhenInUseUsageDescription": "SAR tracks your location in the background to keep the team map accurate during active searches.",
  "UIBackgroundModes": ["location"]
}
```

If any are missing, add them and run `npx expo prebuild -p ios --clean` followed by `npx expo run:ios --device` to rebuild the native app.

---

## Test plan (the only thing that matters)

You've succeeded when:

1. The dev build is on a physical iPhone
2. Two phones (your iPhone + a teammate's) both run the app
3. Both phones connect to the same ngrok URL
4. Both enter a name and join the same test mission
5. Both see their own blue dot on the map
6. On the DGX: `sqlite3 /home/asus/sqlite/mission.db "SELECT user_id, lat, lon, ts FROM pings ORDER BY ts DESC LIMIT 20"` shows ping rows from both users, less than 30 seconds old
7. Lock one phone's screen. Wait 1 minute. Unlock. Verify pings continued to arrive during the locked period.

If all seven pass, Phase 1 is done. Stop and check in before starting Phase 2.

---

## Things that will probably go wrong, and how to handle

- **iOS background location quirks:** Background updates require both the `UIBackgroundModes: ["location"]` entry AND an active foreground task. `expo-task-manager` handles this — follow the official Expo background-location guide exactly. Don't roll your own.
- **ngrok URL changes on restart:** The user re-enters it in the app. That's fine for Phase 1. If it gets annoying, write `tunnel.sh` on the DGX that prints the current URL to a QR code in the terminal, photographed and typed in. Don't build a discovery system.
- **SpatiaLite loading:** Confirm in `api/db.py` that you're loading the SpatiaLite extension on connection. If `pings.geom` is a SpatiaLite POINT, you need `conn.enable_load_extension(True)` and `conn.execute("SELECT load_extension('mod_spatialite')")` before any spatial column queries.
- **Bearer token storage:** `expo-secure-store` is keychain on iOS. Don't use `AsyncStorage` for the token.
- **CORS:** If you're testing from a web browser at any point, add CORS middleware to FastAPI. The phone app doesn't need it.

---

## After Phase 1

Once the seven test criteria pass, the next phases will layer in:

- **Phase 2:** Subcell rendering + auto-mark-searched + `/mission/state` endpoint
- **Phase 3:** Chat system + commander/dispatcher agents
- **Phase 4:** Dashboard + demo polish

Don't build any of those now. Phase 1 first.