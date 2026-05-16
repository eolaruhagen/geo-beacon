-- Agent coordination. There is no invocation queue and no coverage_cache.
--
-- Queue removed: agent_worker polls every ~15s. Each tick it diffs events
-- against missions.last_agent_invocation_ts and decides whether to invoke.
-- High-priority triggers (subject_found, commander_override) set
-- missions.force_agent_invoke = 1 to short-circuit the sleep on the next tick.
--
-- coverage_cache removed: POD per segment is derived directly from hex_visits
-- against hex_cells.segment_id (cheap join, no materialization needed).
-- Spatial worker writes segments.pod in place.

CREATE TABLE agent_journal (
  id              INTEGER PRIMARY KEY,
  mission_id      INTEGER NOT NULL REFERENCES missions(id),
  ts              INTEGER NOT NULL,
  trigger         TEXT    NOT NULL,           -- comma-separated event kinds since last tick
  brief_md        TEXT    NOT NULL,           -- snapshot of Mission Brief input
  tool_calls_json TEXT    NOT NULL,           -- JSON array of {tool, args, result}
  reasoning       TEXT,
  duration_ms     INTEGER NOT NULL
);
CREATE INDEX idx_journal_mission_ts ON agent_journal (mission_id, ts DESC);
