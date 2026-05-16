-- Base non-spatial tables.
-- Spatial tables (pings, segments, findings, hazards) live in 002.
-- Terrain tables (terrain_cells, osm_features) live in 003.
-- Coordination tables (agent_journal, agent_invocation_queue, coverage_cache) live in 004.

CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  display_name  TEXT    NOT NULL,
  callsign      TEXT    UNIQUE,    -- 'Alpha', 'Bravo', ...; null for observers
  phone         TEXT,
  role          TEXT    NOT NULL CHECK (role IN ('searcher', 'team_leader', 'observer')),
  status        TEXT    NOT NULL CHECK (status IN ('standby', 'dispatched', 'on_segment', 'returning', 'no_comms', 'off_duty')) DEFAULT 'standby',
  bearer_token  TEXT    NOT NULL UNIQUE,
  created_ts    INTEGER NOT NULL
);

CREATE TABLE missions (
  id                   INTEGER PRIMARY KEY,
  name                 TEXT    NOT NULL,
  status               TEXT    NOT NULL CHECK (status IN ('planning', 'active', 'subject_found', 'suspended', 'ended')),
  subject_description  TEXT    NOT NULL,
  pls_lat              REAL    NOT NULL,
  pls_lon              REAL    NOT NULL,
  pls_ts               INTEGER NOT NULL,
  started_ts           INTEGER NOT NULL,
  ended_ts             INTEGER
  -- area_geom POLYGON added in 002 via AddGeometryColumn
);

-- dispatches.segment_id references segments(id), created in 002.
-- SQLite allows forward FK references; enforcement happens at INSERT time
-- with PRAGMA foreign_keys=ON, by which point 002 has been applied.
CREATE TABLE dispatches (
  id              INTEGER PRIMARY KEY,
  mission_id      INTEGER NOT NULL REFERENCES missions(id),
  user_id         INTEGER NOT NULL REFERENCES users(id),
  segment_id      INTEGER REFERENCES segments(id),
  sweep_type      TEXT    CHECK (sweep_type IN ('hasty', 'efficient', 'thorough')),
  entry_lat       REAL,
  entry_lon       REAL,
  instruction     TEXT    NOT NULL,
  reasoning       TEXT    NOT NULL,
  status          TEXT    NOT NULL CHECK (status IN ('pending', 'acked', 'in_progress', 'completed', 'cancelled', 'superseded')),
  issued_ts       INTEGER NOT NULL,
  acked_ts        INTEGER,
  started_ts      INTEGER,
  completed_ts    INTEGER,
  superseded_by   INTEGER REFERENCES dispatches(id)
);
CREATE INDEX idx_dispatches_user_status    ON dispatches (user_id, status);
CREATE INDEX idx_dispatches_mission_issued ON dispatches (mission_id, issued_ts DESC);

CREATE TABLE broadcasts (
  id          INTEGER PRIMARY KEY,
  mission_id  INTEGER NOT NULL REFERENCES missions(id),
  scope       TEXT    NOT NULL,  -- 'all' | 'user:{id}'
  kind        TEXT    NOT NULL CHECK (kind IN ('info', 'warning', 'recall', 'finding_alert', 'route_correction')),
  message     TEXT    NOT NULL,
  ts          INTEGER NOT NULL
);
CREATE INDEX idx_broadcasts_mission_ts ON broadcasts (mission_id, ts DESC);
