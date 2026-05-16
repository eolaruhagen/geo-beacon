-- Migration 004 — user-mission association + per-mission callsign uniqueness +
-- nullable finding descriptions.
--
-- Addresses:
--   U-1: users.callsign was globally UNIQUE; spec implies per-mission scope.
--        Add users.current_mission_id and replace global UNIQUE(callsign)
--        with UNIQUE(current_mission_id, callsign).
--   F-2: findings.description was NOT NULL — phone hex-taps without a narrative
--        should be legal. Make it nullable.
--
-- DEV-SETUP ASSUMPTION: data in `users` and `findings` is throwaway, so this
-- migration simply DROPs and recreates both tables instead of doing the
-- rename-and-copy dance. If you ever ship a real deployment, write a proper
-- 005 that backfills first. Foreign keys are toggled OFF for the duration so
-- the references from pings/dispatches/findings to users don't trip during
-- the drop. `findings.geom` is unregistered before drop and re-registered
-- after recreate so SpatiaLite metadata stays consistent.

PRAGMA foreign_keys = OFF;

-- ----- users -----
DROP TABLE users;

CREATE TABLE users (
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

CREATE INDEX idx_users_mission ON users (current_mission_id);

-- ----- findings -----
SELECT DiscardGeometryColumn('findings', 'geom');
DROP TABLE findings;

CREATE TABLE findings (
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

SELECT AddGeometryColumn('findings', 'geom', 4326, 'POINT', 'XY', 1);
SELECT CreateSpatialIndex('findings', 'geom');
CREATE INDEX idx_findings_mission_ts ON findings (mission_id, ts DESC);
CREATE INDEX idx_findings_hex        ON findings (hex_id);

PRAGMA foreign_keys = ON;
