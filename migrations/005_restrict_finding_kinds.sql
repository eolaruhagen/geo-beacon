-- Remove generic finding kinds from the field-report contract.
--
-- Fresh databases already get this CHECK from 002_spatial.sql. This migration
-- rebuilds existing findings tables so old DBs also reject `note` and `other`.
--
-- Existing legacy `note`/`other` rows are preserved as `clue` rows with a
-- description prefix. That keeps historical text visible while preventing new
-- generic reports from entering the database.

SELECT DisableSpatialIndex('findings', 'geom');
DROP TABLE IF EXISTS idx_findings_geom;
SELECT DiscardGeometryColumn('findings', 'geom');

ALTER TABLE findings RENAME TO findings_old;

CREATE TABLE findings (
  id                INTEGER PRIMARY KEY,
  mission_id        INTEGER NOT NULL REFERENCES missions(id),
  reporter_user_id  INTEGER NOT NULL REFERENCES users(id),
  hex_id            INTEGER NOT NULL REFERENCES hex_cells(id),
  ts                INTEGER NOT NULL,
  lat               REAL    NOT NULL,
  lon               REAL    NOT NULL,
  kind              TEXT    NOT NULL CHECK (kind IN ('clue', 'subject_found', 'subject_sighting', 'hazard', 'footprint', 'discarded_item')),
  description       TEXT,
  confidence        REAL    NOT NULL,
  photo_url         TEXT
);
SELECT AddGeometryColumn('findings', 'geom', 4326, 'POINT', 'XY', 1);
SELECT CreateSpatialIndex('findings', 'geom');

INSERT INTO findings (
  id, mission_id, reporter_user_id, hex_id, ts, lat, lon,
  kind, description, confidence, photo_url, geom
)
SELECT
  id,
  mission_id,
  reporter_user_id,
  hex_id,
  ts,
  lat,
  lon,
  CASE
    WHEN kind IN ('note', 'other') THEN 'clue'
    ELSE kind
  END AS kind,
  CASE
    WHEN kind IN ('note', 'other') THEN '[legacy generic finding] ' || COALESCE(description, '')
    ELSE description
  END AS description,
  confidence,
  photo_url,
  SetSRID(MakePoint(lon, lat), 4326)
FROM findings_old;

DROP TABLE findings_old;

CREATE INDEX idx_findings_mission_ts ON findings (mission_id, ts DESC);
CREATE INDEX idx_findings_hex        ON findings (hex_id);
