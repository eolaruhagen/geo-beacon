-- Terrain + OSM cache tables, populated by scripts/fetch_terrain.py per mission bbox.

CREATE TABLE terrain_cells (
  id              INTEGER PRIMARY KEY,
  mission_id      INTEGER NOT NULL REFERENCES missions(id),
  center_elev_m   REAL    NOT NULL,
  avg_slope_deg   REAL    NOT NULL,
  dominant_cover  TEXT    NOT NULL CHECK (dominant_cover IN ('open', 'mixed', 'dense', 'water', 'rock'))
);
SELECT AddGeometryColumn('terrain_cells', 'geom', 4326, 'POLYGON', 'XY', 1);
SELECT CreateSpatialIndex('terrain_cells', 'geom');
CREATE INDEX idx_terrain_mission ON terrain_cells (mission_id);

-- osm_features: mixed-geometry cache (LINESTRING for trails/roads, POLYGON for water/building).
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
