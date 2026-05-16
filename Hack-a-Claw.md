### **My ai slop for this**

### 

### **1\. Goals**

Define the data layer that sits between FastAPI ingestion endpoints and the OpenClaw agent. The richness of this layer bounds the agent's reasoning quality. Specifically:

* Turn \~1500 raw GPS pings/day into \~50 semantically meaningful events  
* Provide the agent with a structured "current state brief" rather than raw data  
* Maintain history sufficient for pattern detection and drift reasoning  
* Keep the agent's tool surface narrow and debuggable

  ### **2\. Storage**

**SQLite database at `/data/agent.db`** on the DGX. WAL mode for concurrent access from FastAPI and the agent worker. Schema is the source of truth; agent reads through a curated skill interface, never raw SQL.

Rationale: SQLite is sufficient for single-user scale (\~10K pings over the demo period). PostGIS / Postgres is not needed and would add infrastructure cost.

### **3\. The Five-Layer Model**

Data flows upward through five layers, each more semantic than the last. The agent reads only Layer 5\.

#### **Layer 0 — `location_pings` (raw, never shown to agent)**

Append-only. Source of truth.

- id              INTEGER PRIMARY KEY  
  - user\_id         TEXT  
  - ts              INTEGER         \-- unix ms  
  - lat             REAL  
  - lon             REAL  
  - accuracy\_m      REAL  
  - speed\_mps       REAL NULL  
  - \-- From phone payload (already provided):  
  - street          TEXT NULL  
  - zip             TEXT NULL  
  - state           TEXT NULL  
  - region          TEXT NULL  
  - source          TEXT            \-- "phone" | "seeded" | "demo\_override"  
    INDEX (user\_id, ts)

    #### **Layer 1 — `candidate_places` → `places`**

**Two-stage promotion.** A spatial cluster becomes a `candidate_place` immediately. It promotes to `place` only after meeting a recurrence threshold (≥2 distinct days OR ≥30 min total dwell across ≥2 visits). Prevents one-off stops from polluting the agent's view.

- **candidate\_places**:  
  - id              INTEGER PRIMARY KEY  
  - user\_id         TEXT  
  - center\_lat      REAL  
  - center\_lon      REAL  
  - radius\_m        REAL  
  - first\_seen\_ts   INTEGER  
  - last\_seen\_ts    INTEGER  
  - visit\_count     INTEGER  
  - total\_dwell\_s   INTEGER  
  - distinct\_days   INTEGER  
  - promoted\_place\_id INTEGER NULL   \-- foreign key once promoted  
  -   
  - **places:**  
  - id              INTEGER PRIMARY KEY  
  - user\_id         TEXT  
  - name            TEXT             \-- derived from address fields \+ heuristics  
  - display\_name    TEXT NULL        \-- user override  
  - center\_lat      REAL  
  - center\_lon      REAL  
  - radius\_m        REAL  
  - inferred\_type   TEXT             \-- 'home'|'school'|'work'|'cafe'|'gym'|'transit'|'residence'|'unknown'  
  - address\_street  TEXT NULL  
  - address\_zip     TEXT NULL  
  - address\_region  TEXT NULL  
  - first\_visit\_ts  INTEGER  
  - last\_visit\_ts   INTEGER  
  - total\_visits    INTEGER  
  - total\_dwell\_s   INTEGER  
    INDEX (user\_id, last\_visit\_ts DESC)

**Clustering:** DBSCAN on `(lat, lon)` with haversine metric, `eps=50m`, `min_samples=5`. Run by background worker every 60s on recent unassigned pings.

**Naming heuristics (in priority order):**

1. **User override** (`display_name`) — highest priority  
2. **Time-of-day pattern wins for `home`/`school`** — if 80%+ of overnight stays (midnight-6am) → "Home"; if 70%+ of weekday 9am-3pm presence → "School" (you mentioned you're a UCSC junior, configure accordingly)  
3. **Address fields from phone payload** — `{street}` if available  
4. **Fallback** — `"Unknown place near {region}"`

Since the phone payload already includes street/zip/state/region, **no reverse geocoding API is needed.** This is a real simplification — remove Nominatim/Mapbox from the design.

#### **Layer 2 — `visits` (the agent's bread and butter)**

A visit is one period of presence at one place. This is what the agent reads most.

- visits:  
  - id              INTEGER PRIMARY KEY  
  - user\_id         TEXT  
  - place\_id        INTEGER NULL     \-- null \= unmatched stationary period  
  - arrival\_ts      INTEGER  
  - departure\_ts    INTEGER NULL     \-- null \= currently here  
  - duration\_s      INTEGER NULL  
  - linked\_event\_id INTEGER NULL     \-- calendar event overlapping this visit  
  - ping\_count      INTEGER          \-- pings that contributed  
  - INDEX (user\_id, arrival\_ts DESC)  
    INDEX (place\_id)

**Visit detection (state machine in background worker):**

* Start: ≥3 consecutive pings within a place's radius  
* End: ≥2 consecutive pings outside, OR ≥10 min ping gap, OR ping from new place  
* Backfill: on each cycle, close any open visit whose last contributing ping is \>10 min old

  #### **Layer 3 — `trips` (movement between visits)**

  - trips:  
  - id              INTEGER PRIMARY KEY  
  - user\_id         TEXT  
  - from\_place\_id   INTEGER NULL     \-- null for trips starting outside known places  
  - to\_place\_id     INTEGER NULL  
  - departure\_ts    INTEGER  
  - arrival\_ts      INTEGER  
  - duration\_s      INTEGER  
  - distance\_m      REAL             \-- sum of haversine between consecutive pings  
  - avg\_speed\_mps   REAL  
  - inferred\_mode   TEXT             \-- 'walk'|'bike'|'drive'|'transit'|'unknown'  
  - ping\_count      INTEGER  
    INDEX (user\_id, departure\_ts DESC)

**Mode inference:** thresholds on avg speed (walk \<2 m/s, bike 2-7, drive \>8) — primitive but good enough for v1.

**Out of scope for hackathon:** route matching against road networks. "You took this road vs. that road" requires a routing graph or map-matching that's not feasible in 24 hours. We retain enough info (distance, duration, mode) to *say* "drives to UCSC typically take 18 min" without naming the road.

#### **Layer 4 — `patterns` (recurring shapes of life)**

Recomputed periodically (every few hours, or on-demand pre-invocation). This is where the *interesting* reasoning material lives.

- patterns:  
  - id              INTEGER PRIMARY KEY  
  - user\_id         TEXT  
  - pattern\_type    TEXT             \-- see below  
  - subject\_id      INTEGER NULL     \-- place\_id, friend\_id, etc. depending on type  
  - description     TEXT             \-- human-readable summary  
  - data            TEXT             \-- JSON with type-specific fields  
  - confidence      REAL             \-- 0.0-1.0  
  - first\_observed  INTEGER  
  - last\_updated    INTEGER  
    INDEX (user\_id, pattern\_type)

**Pattern types (build in priority order — only the first 3 are required for the demo):**

1. **`regular_visit`** — recurring visits to a place. Data: `{place_id, days_of_week, typical_hour_range, typical_duration_min, weeks_observed}`  
2. **`absence`** — established place not visited recently. Data: `{place_id, usual_cadence_days, days_since_last_visit, deviation_score}`. **Drift detection lives here.**  
3. **`recent_intent`** — pulled from `user_state` entries, surfaced as a pattern. Data: `{text, ts, decay_days}`  
4. **`co_visit`** — places usually visited together in a trip chain (Peet's → home, library → Iveta)  
5. **`companion`** — places usually visited when friend X is also nearby

   #### **Layer 5 — The Brief**

The structured markdown document passed to the agent as the primary input on invocation. **Programmatically generated, deterministic, debuggable.** Not LLM-generated.

Example structure (template fills from the layers above):

markdown

- **\# State Brief — {user\_name} — {now\_local}**  
  -   
  - **\#\# Right Now**  
  - \- Currently at: {place.name} ({place.inferred\_type}), arrived {visit.arrival\_local} ({dwell\_min} min ago)  
  - \- Previously: {prev\_visit.place.name}, {prev\_visit.arrival\_local}–{prev\_visit.departure\_local}  
  -   
  - **\#\# Last 12 Hours**  
  - {for each visit in window:}  
  - \- {arrival\_local} {place.name} ({duration\_min} min){if linked\_event: ", during ‘"+event.title+"’"}  
  -   
  - **\#\# Active Patterns**  
  - {regular visits matching today:}  
  - \- Regular: {pattern.description} ← matches current  
  - {absences flagged:}  
  - \- ⚠ Absence: {pattern.description}  
  - {recent intents:}  
  - \- Recent intent: "{user\_state.text}" ({user\_state.relative\_time})  
  -   
  - **\#\# Upcoming Calendar (next 6h)**  
  - {for each event:}  
  - \- {event.start\_local}: {event.title}{if event.location: " @ "+event.location}  
  -   
  - **\#\# Friends**  
  - {for each friend with location data:}  
  - \- {friend.name}: at {friend.current\_place} (since {friend.arrival\_local}, {distance\_from\_user} from you)  
  -   
  - **\#\# Recent Agent Notes (last 72h)**  
  - {for each journal\_entry, latest first, max 10:}  
    \- ({entry.relative\_time}, {entry.kind}): {entry.content}

Target size: 300-700 tokens. If a section is empty, omit the header.

### **4\. Other Tables**

#### **`calendar_events`**

Synced from Google Calendar by a separate worker every 15 min. Agent reads from this, never calls Google directly.

- id              INTEGER PRIMARY KEY  
  - user\_id         TEXT  
  - external\_id     TEXT             \-- Google's event id  
  - title           TEXT  
  - description     TEXT NULL  
  - start\_ts        INTEGER  
  - end\_ts          INTEGER  
  - location        TEXT NULL  
  - attendees\_json  TEXT             \-- JSON array  
  - last\_synced\_ts  INTEGER  
    INDEX (user\_id, start\_ts)  
    

    #### **`suggestions`**

  - id              INTEGER PRIMARY KEY  
  - user\_id         TEXT  
  - created\_ts      INTEGER  
  - kind            TEXT             \-- 'hangout\_opportunity'|'drift\_check\_in'|'departure\_reminder'|...  
  - text            TEXT             \-- message phrased for user  
  - action\_json     TEXT NULL        \-- e.g. {"type":"open\_maps","query":"Scotts Valley Coffee"}  
  - reasoning       TEXT             \-- transparency / demo  
    status          TEXT             \-- 'pending'|'accepted'|'dismissed'|'expired'

    #### **`location_events` (audit \+ replay)**

Every `/location/event` call is logged here regardless of whether it triggered the agent.

- id              INTEGER PRIMARY KEY  
  - user\_id         TEXT  
  - ts              INTEGER  
  - lat             REAL  
  - lon             REAL  
  - trigger\_reason  TEXT             \-- which gate rule fired, or 'no\_trigger'  
    invoked\_agent   INTEGER          \-- 1 if agent ran

    ### **5\. FastAPI Endpoints**

All endpoints require `X-API-Key` header. Plain HTTP behind ngrok (which terminates TLS at its edge).

#### **Ingestion**

`POST /location/ping`

* Body: `{lat, lon, ts?, accuracy_m, speed_mps?, street?, zip?, state?, region?}`  
* Append to `location_pings`. Return 200 immediately.

`POST /location/event`

* Body: same as ping, plus `transition_type?`  
* Append to `location_pings` AND `location_events`. Run gate. If gate passes, enqueue agent invocation. Return `{invoked: bool, reason: string}`.  
* 

`POST /calendar/sync (this may want to live on its own Python process)`

* Trigger immediate Google Calendar resync.

`POST /demo/invoke`

* Body: `{reason?}` — forces agent invocation regardless of gate. For live demo control.

  #### **Read (for dashboard)**

`GET /suggestions?status=pending`  
 `POST /suggestions/{id}/accept` — may trigger iPhone command  
 `POST /suggestions/{id}/dismiss` 

### **6\. Agent Invocation Gate**

Rules are evaluated in order on every `POST /location/event`. First match wins.

1. `manual_demo` — request hit `/demo/invoke` or query param `?force=true` → invoke  
2. `place_transition` — current `place_id` ≠ last event's `place_id` → invoke  
3. `new_place_discovered` — clustering created a new place from these pings → invoke  
4. `dwell_threshold` — at current place \>15 min and no agent invocation in last 30 min → invoke  
5. `calendar_proximity` — calendar event starts or ends within the next 15 min → invoke  
6. `Heartbeat_fallback –`\>90 min since last invocation → invoke  
7. otherwise → drop, `reason: "no_trigger"`

Gate logic lives in FastAPI's `/location/event` handler. \~30 lines of Python.

### **7\. Agent Tool Interface (Skills)**

The agent does NOT have raw SQLite access. Skills are explicit, typed, and audited.

#### **Read skills**

* `get_place_details(place_id_or_name)` → place row \+ recent visits  
* `get_visits_at_place(place_id_or_name, days_back=30)` → list of visits  
* `query_calendar(start_ts, end_ts)` → list of events  
* `get_user_state(days=7)` → list of journal entries

  #### **Write skills**

* `create_suggestion(kind, text, action?, reasoning)` → append to `suggestions`, status=pending  
* `update_friend_notes(friend_id, notes)` → update agent-maintained friend notes

  ### **8\. Background Workers**

Three workers run as their own py processes, just a shell script in tmux running in sleep(N) loops,  all reading/writing the same SQLite:

1. **Clustering worker** — every 60s, processes recent pings into candidate\_places/places, runs visit detection. Owns Layers 0→1→2→3.  
2. **Pattern worker** — every 10 min, recomputes Layer 4 patterns over recent visits. Cheap; just aggregate queries.  
3. **Calendar sync worker** — every 15 min, pulls Google Calendar for next 7 days \+ past 1 day, upserts into `calendar_events`. The initial OAuth flow happens once at setup.  
4. **Agent worker** — consumes the invocation queue (populated by the gate). Each task: load brief, run agent reasoning, persist journal/suggestions.  
     
   - 