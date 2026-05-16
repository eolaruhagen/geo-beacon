-- Hex grid + OSM feature cache. The hex grid IS the terrain layer at finer
-- resolution than segments; there is no separate terrain_cells table.
--
-- hex_cells (~30m corner-to-corner, ~5k cells per 2km x 2km mission area)
-- carries three classes of state:
--
--   1. Immutable terrain truth (center_elev_m, slope_deg, dominant_cover)
--      derived from USGS NED DEM + ESA WorldCover at seed time by
--      scripts/fetch_terrain.py.
--
--   2. OSM-rasterized booleans (has_trail, has_road, is_building, is_water)
--      derived by spatially intersecting each hex with osm_features at seed.
--
--   3. Runtime flags (flag_danger, flag_impassable, flag_clue, flag_poi)
--      maintained by the skill layer: when a hazard polygon is written, the
--      affected hexes' flag_danger is set; when a finding is logged, the
--      containing hex's flag_clue is set. The boolean is the fast-render
--      cache; the source-of-truth metadata lives in hazards/findings.
--
-- searchable is a derived view-like helper: read NOT is_building AND NOT
-- is_water AND NOT flag_impassable at query time. We don't store it as a
-- column because it'd need to be kept in sync with three inputs (race-prone).
-- POD per segment = visited_searchable_hexes / total_searchable_hexes.

CREATE TABLE hex_cells (
  id              INTEGER PRIMARY KEY,
  mission_id      INTEGER NOT NULL REFERENCES missions(id),
  segment_id      INTEGER NOT NULL REFERENCES segments(id),

  -- immutable terrain truth (was terrain_cells)
  center_elev_m   REAL    NOT NULL,
  slope_deg       REAL    NOT NULL,
  dominant_cover  TEXT    NOT NULL CHECK (dominant_cover IN ('open', 'mixed', 'dense', 'water', 'rock', 'built')),

  -- OSM-rasterized booleans (set at seed by spatial join against osm_features)
  has_trail       INTEGER NOT NULL DEFAULT 0,
  has_road        INTEGER NOT NULL DEFAULT 0,
  is_building     INTEGER NOT NULL DEFAULT 0,
  is_water        INTEGER NOT NULL DEFAULT 0,

  -- runtime flags (set by skill layer on hazard/finding writes)
  flag_danger       INTEGER NOT NULL DEFAULT 0,    -- any active hazard intersects this hex
  flag_impassable   INTEGER NOT NULL DEFAULT 0,    -- volunteer- or agent-reported obstacle
  flag_clue         INTEGER NOT NULL DEFAULT 0,    -- a finding exists in this hex
  flag_poi          INTEGER NOT NULL DEFAULT 0,    -- agent or commander marked for investigation
  flags_updated_ts  INTEGER
);
SELECT AddGeometryColumn('hex_cells', 'geom', 4326, 'POLYGON', 'XY', 1);
SELECT CreateSpatialIndex('hex_cells', 'geom');
CREATE INDEX idx_hex_mission         ON hex_cells (mission_id);
CREATE INDEX idx_hex_segment         ON hex_cells (segment_id);
CREATE INDEX idx_hex_flag_danger     ON hex_cells (mission_id, flag_danger) WHERE flag_danger = 1;
CREATE INDEX idx_hex_flag_impassable ON hex_cells (mission_id, flag_impassable) WHERE flag_impassable = 1;
CREATE INDEX idx_hex_flag_clue       ON hex_cells (mission_id, flag_clue) WHERE flag_clue = 1;
CREATE INDEX idx_hex_flag_poi        ON hex_cells (mission_id, flag_poi) WHERE flag_poi = 1;

-- hex_visits: append-only log of (hex, user, ts). Source of truth for coverage.
-- Spatial worker tick: for each new ping since last_tick, point-in-hex lookup,
-- INSERT OR IGNORE one row per (hex_id, user_id) per ping. Recompute segment
-- POD from the count of distinct visited hexes per segment.
CREATE TABLE hex_visits (
  id        INTEGER PRIMARY KEY,
  hex_id    INTEGER NOT NULL REFERENCES hex_cells(id),
  user_id   INTEGER NOT NULL REFERENCES users(id),
  ts        INTEGER NOT NULL
);
CREATE INDEX idx_hex_visits_hex    ON hex_visits (hex_id);
CREATE INDEX idx_hex_visits_user_ts ON hex_visits (user_id, ts);

-- osm_features: mixed-geometry cache (LINESTRING for trails/roads, POLYGON
-- for water/building). Kept as a separate table even though hex_cells carries
-- the rasterized booleans, because:
--   - query_route needs the original line geometry for ST_ClosestPoint snap
--   - map renderers want trail centerlines, not hex-colored ribbons
-- Stored as generic GEOMETRY since SpatiaLite enforces one type per column otherwise.
CREATE TABLE osm_features (
  id          INTEGER PRIMARY KEY,
  mission_id  INTEGER NOT NULL REFERENCES missions(id),
  kind        TEXT    NOT NULL CHECK (kind IN ('trail', 'road', 'water', 'building')),
  name        TEXT
);
SELECT AddGeometryColumn('osm_features', 'geom', 4326, 'GEOMETRY', 'XY', 1);
SELECT CreateSpatialIndex('osm_features', 'geom');
CREATE INDEX idx_osm_mission_kind ON osm_features (mission_id, kind);
