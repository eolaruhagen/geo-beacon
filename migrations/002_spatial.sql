-- Spatial tables + geometry columns on missions.
-- Requires mod_spatialite loaded into the sqlite connection (handled by
-- the migration runner before this script runs).

SELECT InitSpatialMetaData(1);

-- Geometry column on missions.area_geom
SELECT AddGeometryColumn('missions', 'area_geom', 4326, 'POLYGON', 'XY', 1);
SELECT CreateSpatialIndex('missions', 'area_geom');

-- pings: raw GPS, POINT geometry
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

-- segments: search sectors, POLYGON geometry
CREATE TABLE segments (
  id                INTEGER PRIMARY KEY,
  mission_id        INTEGER NOT NULL REFERENCES missions(id),
  name              TEXT    NOT NULL,
  area_m2           REAL    NOT NULL,
  poa               REAL    NOT NULL,
  pod               REAL    NOT NULL DEFAULT 0,
  pos               REAL    NOT NULL DEFAULT 0,
  status            TEXT    NOT NULL CHECK (status IN ('unassigned', 'assigned', 'in_progress', 'swept', 'cleared')),
  sweep_type        TEXT    CHECK (sweep_type IN ('hasty', 'efficient', 'thorough')),
  target_pod        REAL,
  avg_slope_deg     REAL    NOT NULL,
  dominant_cover    TEXT    NOT NULL CHECK (dominant_cover IN ('open', 'mixed', 'dense', 'water', 'rock')),
  trail_length_m    REAL    NOT NULL DEFAULT 0,
  UNIQUE (mission_id, name)
);
SELECT AddGeometryColumn('segments', 'geom', 4326, 'POLYGON', 'XY', 1);
SELECT CreateSpatialIndex('segments', 'geom');
CREATE INDEX idx_segments_mission_status ON segments (mission_id, status);

-- findings: reported by searchers, POINT geometry
CREATE TABLE findings (
  id                INTEGER PRIMARY KEY,
  mission_id        INTEGER NOT NULL REFERENCES missions(id),
  reporter_user_id  INTEGER NOT NULL REFERENCES users(id),
  ts                INTEGER NOT NULL,
  lat               REAL    NOT NULL,
  lon               REAL    NOT NULL,
  kind              TEXT    NOT NULL CHECK (kind IN ('clue', 'subject_found', 'subject_sighting', 'hazard', 'footprint', 'discarded_item', 'other')),
  description       TEXT    NOT NULL,
  confidence        REAL    NOT NULL,
  photo_url         TEXT    -- deferred for hack; column present so we don't migrate later
);
SELECT AddGeometryColumn('findings', 'geom', 4326, 'POINT', 'XY', 1);
SELECT CreateSpatialIndex('findings', 'geom');
CREATE INDEX idx_findings_mission_ts ON findings (mission_id, ts DESC);

-- hazards: agent- or commander-flagged dangers, POLYGON geometry
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
