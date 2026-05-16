-- Phase 2 auto-mark-searched: when a searcher's ping lands inside a hex cell,
-- the cell is marked covered. We persist coverage directly on hex_cells
-- (rather than a separate hex_visits table) for demo simplicity — one extra
-- UPDATE per ping, single source of truth. A hex_visits table remains a
-- forward-compat option if we ever need per-user / time-series coverage.
--
-- Columns:
--   flag_searched          0/1 — set on first ping that lands in the cell.
--                          Never reset by v1 logic; the agent may later own
--                          a reset path if coverage needs to be invalidated.
--   searched_by_user_id    Last user to ping inside the cell (last-writer-wins).
--                          Reserved for future per-searcher color attribution.
--   searched_ts            Unix seconds of the most recent ping that updated
--                          this cell. Reserved for future time-decay visuals.

ALTER TABLE hex_cells ADD COLUMN flag_searched       INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hex_cells ADD COLUMN searched_by_user_id INTEGER REFERENCES users(id);
ALTER TABLE hex_cells ADD COLUMN searched_ts         INTEGER;

CREATE INDEX idx_hex_cells_searched ON hex_cells (mission_id, flag_searched);
