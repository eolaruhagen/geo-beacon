-- Agent coordination plumbing: invocation queue, journal, coverage cache.

CREATE TABLE agent_invocation_queue (
  id          INTEGER PRIMARY KEY,
  mission_id  INTEGER NOT NULL REFERENCES missions(id),
  trigger     TEXT    NOT NULL,
  context     TEXT,           -- JSON, e.g. {"finding_id": 42}
  created_ts  INTEGER NOT NULL,
  claimed_ts  INTEGER         -- NULL = unclaimed; set when agent_worker picks it up
);
CREATE INDEX idx_queue_unclaimed ON agent_invocation_queue (mission_id, created_ts)
  WHERE claimed_ts IS NULL;

CREATE TABLE agent_journal (
  id              INTEGER PRIMARY KEY,
  mission_id      INTEGER NOT NULL REFERENCES missions(id),
  ts              INTEGER NOT NULL,
  trigger         TEXT    NOT NULL,
  brief_md        TEXT    NOT NULL,    -- snapshot of Mission Brief input
  tool_calls_json TEXT    NOT NULL,    -- JSON array of {tool, args, result}
  reasoning       TEXT,
  duration_ms     INTEGER NOT NULL
);
CREATE INDEX idx_journal_mission_ts ON agent_journal (mission_id, ts DESC);

-- coverage_cache: one row per segment, rewritten by spatial_worker each tick.
CREATE TABLE coverage_cache (
  segment_id        INTEGER PRIMARY KEY REFERENCES segments(id),
  covered_area_m2   REAL    NOT NULL,
  pod_current       REAL    NOT NULL,
  last_computed_ts  INTEGER NOT NULL
);
SELECT AddGeometryColumn('coverage_cache', 'covered_geom', 4326, 'MULTIPOLYGON', 'XY', 0);
SELECT CreateSpatialIndex('coverage_cache', 'covered_geom');
