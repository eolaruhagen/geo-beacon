-- Migration 004 — user-mission association + per-mission callsign uniqueness +
-- nullable finding descriptions.
--
-- Addresses:
--   U-1: users.callsign was globally UNIQUE; spec implies per-mission scope.
--        Add users.current_mission_id and replace global UNIQUE(callsign)
--        with UNIQUE(current_mission_id, callsign). The single-mission
--        scope per spec §2 means we never have two simultaneous missions in
--        practice, but the constraint matches the intent.
--   F-2: findings.description was NOT NULL but most clue / hex-tap finds carry
--        no narrative text — phone keyboard taps with a kind enum should be
--        legal. Make it nullable.
--
-- SQLite can't ALTER an existing column's constraints, so both `users` and
-- `findings` are rebuilt via the rename-and-copy pattern. `findings.geom` is
-- handled via DiscardGeometryColumn / AddGeometryColumn around the rebuild so
-- SpatiaLite's metadata stays consistent.
--
-- Foreign keys are toggled OFF for the rebuild so the temporary table-name
-- shuffle doesn't trip the references from pings, dispatches, findings, etc.

PRAGMA foreign_keys = OFF;

-- ----- users rebuild -----
CREATE TABLE users_new (
  id                  INTEGER PRIMARY KEY,
  display_name        TEXT    NOT NULL,
  callsign            TEXT,
  phone               TEXT,
  role                TEXT    NOT NULL CHECK (role IN ('searcher', 'observer')) DEFAULT 'searcher',
  status              TEXT    NOT NULL CHECK (status IN ('standby', 'dispatched', 'on_segment', 'returning', 'no_comms', 'off_duty')) DEFAULT 'standby',
  bearer_token        TEXT    NOT NULL UNIQUE,
  current_mission_id  INTEGER REFERENCES missions(id),
  created_ts          INTEGER NOT NULL,
  UNIQUE (current_mission_id, callsign)
);

INSERT INTO users_new (id, display_name, callsign, phone, role, status, bearer_token, current_mission_id, created_ts)
  SELECT id, display_name, callsign, phone, role, status, bearer_token, NULL, created_ts FROM users;

DROP TABLE users;
ALTER TABLE users_new RENAME TO users;

CREATE INDEX idx_users_mission ON users (current_mission_id);

-- ----- findings rebuild -----
SELECT DiscardGeometryColumn('findings', 'geom');
DROP INDEX IF EXISTS idx_findings_mission_ts;
DROP INDEX IF EXISTS idx_findings_hex;

CREATE TABLE findings_new (
  id                INTEGER PRIMARY KEY,
  mission_id        INTEGER NOT NULL REFERENCES missions(id),
  reporter_user_id  INTEGER NOT NULL REFERENCES users(id),
  hex_id            INTEGER NOT NULL REFERENCES hex_cells(id),
  ts                INTEGER NOT NULL,
  lat               REAL    NOT NULL,
  lon               REAL    NOT NULL,
  kind              TEXT    NOT NULL CHECK (kind IN ('clue', 'subject_found', 'subject_sighting', 'hazard', 'footprint', 'discarded_item', 'note', 'other')),
  description       TEXT,
  confidence        REAL    NOT NULL,
  photo_url         TEXT
);

INSERT INTO findings_new (id, mission_id, reporter_user_id, hex_id, ts, lat, lon, kind, description, confidence, photo_url)
  SELECT id, mission_id, reporter_user_id, hex_id, ts, lat, lon, kind, description, confidence, photo_url FROM findings;

DROP TABLE findings;
ALTER TABLE findings_new RENAME TO findings;

SELECT AddGeometryColumn('findings', 'geom', 4326, 'POINT', 'XY', 1);
SELECT CreateSpatialIndex('findings', 'geom');
CREATE INDEX idx_findings_mission_ts ON findings (mission_id, ts DESC);
CREATE INDEX idx_findings_hex        ON findings (hex_id);

PRAGMA foreign_keys = ON;
