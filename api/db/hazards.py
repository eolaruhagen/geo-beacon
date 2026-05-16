from __future__ import annotations

import json
import time

from api.db import session

VALID_KINDS = {"cliff", "water", "weather", "no_comms_zone", "wildlife", "other"}
VALID_SEVERITIES = {"info", "caution", "critical"}


def bulk_insert_hazards(mission_id: int, hazards: list[dict]) -> list[int]:
    """Insert hazards in one transaction. Each row dict needs:
      poly_geojson (Polygon dict), kind, severity, description, expires_ts? (optional).
    Returns list of inserted hazard ids in order."""
    now = int(time.time())
    ids: list[int] = []
    with session() as conn:
        conn.execute("BEGIN")
        for h in hazards:
            if h["kind"] not in VALID_KINDS:
                raise ValueError(f"invalid hazard kind: {h['kind']!r}")
            if h["severity"] not in VALID_SEVERITIES:
                raise ValueError(f"invalid hazard severity: {h['severity']!r}")
            cur = conn.execute(
                """
                INSERT INTO hazards (mission_id, kind, severity, description,
                                     created_ts, expires_ts, geom)
                VALUES (?, ?, ?, ?, ?, ?, SetSRID(GeomFromGeoJSON(?), 4326))
                """,
                (
                    mission_id,
                    h["kind"],
                    h["severity"],
                    h["description"],
                    now,
                    h.get("expires_ts"),
                    json.dumps(h["poly_geojson"]),
                ),
            )
            ids.append(cur.lastrowid)
        conn.execute("COMMIT")
        return ids


def hazards_for_mission(mission_id: int) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            """
            SELECT id, mission_id, kind, severity, description, created_ts, expires_ts,
                   AsGeoJSON(geom) AS poly_geojson
            FROM hazards WHERE mission_id = ?
            """,
            (mission_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d["poly_geojson"] is not None:
                d["poly_geojson"] = json.loads(d["poly_geojson"])
            result.append(d)
        return result


def delete_hazards_for_mission(mission_id: int) -> int:
    """Idempotency helper for re-seeding."""
    with session() as conn:
        cur = conn.execute("DELETE FROM hazards WHERE mission_id = ?", (mission_id,))
        return cur.rowcount or 0
