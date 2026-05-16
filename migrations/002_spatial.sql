-- Spatial tables + geometry columns on missions.
-- Requires mod_spatialite loaded into the sqlite connection (handled by
-- the migration runner before this script runs).
--
-- Segments are the dispatchable unit (~100m) and carry mutable mission state
-- (POA, POD, status, assigned_user_id) plus denormalized terrain summary
-- aggregated from the underlying hex grid at seed time.
--
-- findings.hex_id forward-references hex_cells(id) defined in 003. SQLite
-- allows forward FK references; PRAGMA foreign_keys=ON enforces at INSERT
-- time, by which point 003 has been applied.

SELECT InitSpatialMetaData(1);

-- Geometry column on missions.area_geom
SELECT AddGeometryColumn('missions', 'area_geom', 4326, 'POLYGON', 'XY', 1);
SELECT CreateSpatialIndex('missions', 'area_geom');

-- pings: raw GPS, POINT geometry. Append-only source of truth.
CREATE TABLE pings (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  mission_id    INTEGER NOT NULL REFERENCES missions(id),
  ts            INTEGER NOT NULL,
  lat           REAL    NOT NULL,
  lon           REAL    NOT NULL,
  accuracy_m    REAL,
  speed_mps     REAL,
  battery_pct   INTEGER,
  source        TEXT    NOT NULL CHECK (source IN ('phone', 'replay', 'manual'))
);
SELECT AddGeometryColumn('pings', 'geom', 4326, 'POINT', 'XY', 1);
SELECT CreateSpatialIndex('pings', 'geom');
CREATE INDEX idx_pings_user_ts    ON pings (user_id, ts);
CREATE INDEX idx_pings_mission_ts ON pings (mission_id, ts);

-- segments: search sectors, POLYGON geometry. Terrain stats (avg_slope_deg,
-- dominant_cover, trail_length_m) are aggregated from hex_cells at seed time
-- so the agent can read a single row instead of joining the hex grid.
CREATE TABLE segments (
  id                  INTEGER PRIMARY KEY,
  mission_id          INTEGER NOT NULL REFERENCES missions(id),
  name                TEXT    NOT NULL,
  area_m2             REAL    NOT NULL,
  poa                 REAL    NOT NULL,
  pod                 REAL    NOT NULL DEFAULT 0,
  pos                 REAL    NOT NULL DEFAULT 0,
  status              TEXT    NOT NULL CHECK (status IN ('unassigned', 'assigned', 'in_progress', 'swept', 'cleared')),
  assigned_user_id    INTEGER REFERENCES users(id),
  sweep_type          TEXT    CHECK (sweep_type IN ('hasty', 'efficient', 'thorough')),
  target_pod          REAL,
  avg_slope_deg       REAL    NOT NULL,
  dominant_cover      TEXT    NOT NULL CHECK (dominant_cover IN ('open', 'mixed', 'dense', 'water', 'rock', 'built')),
  trail_length_m      REAL    NOT NULL DEFAULT 0,
  UNIQUE (mission_id, name)
);
SELECT AddGeometryColumn('segments', 'geom', 4326, 'POLYGON', 'XY', 1);
SELECT CreateSpatialIndex('segments', 'geom');
CREATE INDEX idx_segments_mission_status ON segments (mission_id, status);

-- findings: reported by searchers, POINT geometry + hex_id FK.
-- hex_id is set server-side at insert (point-in-polygon lookup if caller
-- provides lat/lon; centroid lookup if caller provides hex_id directly).
-- Both lat/lon AND hex_id are kept: lat/lon for the POA Gaussian bump,
-- hex_id for cheap "findings in this hex" queries and rendering.
CREATE TABLE findings (
  id                INTEGER PRIMARY KEY,
  mission_id        INTEGER NOT NULL REFERENCES missions(id),
  reporter_user_id  INTEGER NOT NULL REFERENCES users(id),
  hex_id            INTEGER NOT NULL REFERENCES hex_cells(id),   -- forward ref; resolved in 003
  ts                INTEGER NOT NULL,
  lat               REAL    NOT NULL,
  lon               REAL    NOT NULL,
  kind              TEXT    NOT NULL CHECK (kind IN ('clue', 'subject_found', 'subject_sighting', 'hazard', 'footprint', 'discarded_item', 'note', 'other')),
  description       TEXT    NOT NULL,
  confidence        REAL    NOT NULL,
  photo_url         TEXT    -- deferred for hack; column present so we don't migrate later
);
SELECT AddGeometryColumn('findings', 'geom', 4326, 'POINT', 'XY', 1);
SELECT CreateSpatialIndex('findings', 'geom');
CREATE INDEX idx_findings_mission_ts ON findings (mission_id, ts DESC);
CREATE INDEX idx_findings_hex        ON findings (hex_id);

-- hazards: runtime metadata-rich annotations (weather, no-comms zones,
-- wildlife sightings, volunteer-reported obstacles). Polygon geometry may
-- span many hexes; the skill layer rasterizes affected hexes' flag_danger
-- column at write time. For demo phase 1, hazards are populated statically
-- at mission init only; runtime flagging UI is a follow-up.
--
-- Seed-time terrain hazards (cliffs, buildings, water) do NOT go in this
-- table; they live as flags directly on hex_cells (is_building, is_water,
-- flag_impassable). Hazards is for things that need a description,
-- expiration, or severity rationale beyond what a boolean can carry.
CREATE TABLE hazards (
  id              INTEGER PRIMARY KEY,
  mission_id      INTEGER NOT NULL REFERENCES missions(id),
  kind            TEXT    NOT NULL CHECK (kind IN ('cliff', 'water', 'weather', 'no_comms_zone', 'wildlife', 'other')),
  severity        TEXT    NOT NULL CHECK (severity IN ('info', 'caution', 'critical')),
  description     TEXT    NOT NULL,
  created_ts      INTEGER NOT NULL,
  expires_ts      INTEGER
);
SELECT AddGeometryColumn('hazards', 'geom', 4326, 'POLYGON', 'XY', 1);
SELECT CreateSpatialIndex('hazards', 'geom');
CREATE INDEX idx_hazards_mission ON hazards (mission_id);
