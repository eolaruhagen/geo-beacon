from __future__ import annotations

import json
import time

from api.db import session

# HexCellRow keys: poly_geojson, segment_id, center_elev_m, slope_deg,
#                  dominant_cover, has_trail, has_road, is_building, is_water
HexCellRow = dict


def bulk_insert_hex_cells(mission_id: int, rows: list[HexCellRow]) -> int:
    """Insert hex cells in one transaction. Returns count inserted."""
    with session() as conn:
        conn.execute("BEGIN")
        for row in rows:
            conn.execute(
                """
                INSERT INTO hex_cells (mission_id, segment_id, center_elev_m,
                                       slope_deg, dominant_cover,
                                       has_trail, has_road, is_building, is_water,
                                       geom)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                        SetSRID(GeomFromGeoJSON(?), 4326))
                """,
                (
                    mission_id,
                    row["segment_id"],
                    row["center_elev_m"],
                    row["slope_deg"],
                    row["dominant_cover"],
                    int(row.get("has_trail", 0)),
                    int(row.get("has_road", 0)),
                    int(row.get("is_building", 0)),
                    int(row.get("is_water", 0)),
                    json.dumps(row["poly_geojson"]),
                ),
            )
        conn.execute("COMMIT")
        return len(rows)


def hex_cells_for_mission(mission_id: int) -> list[dict]:
    """Includes geom as GeoJSON dict (key 'poly_geojson') and all flag columns."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT id, mission_id, segment_id, center_elev_m, slope_deg,
                   dominant_cover, has_trail, has_road, is_building, is_water,
                   flag_danger, flag_impassable, flag_clue, flag_poi,
                   flags_updated_ts, AsGeoJSON(geom) AS poly_geojson
            FROM hex_cells WHERE mission_id = ?
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


def rasterize_hazard_to_hex_flags(mission_id: int, hazard_id: int) -> int:
    """SET flag_danger=1 on hex_cells that ST_Intersects the given hazard polygon.
    Returns count of hex_cells flagged."""
    now = int(time.time())
    with session() as conn:
        cur = conn.execute(
            """
            UPDATE hex_cells
            SET flag_danger = 1, flags_updated_ts = ?
            WHERE mission_id = ?
              AND ST_Intersects(geom, (
                  SELECT geom FROM hazards WHERE id = ?
              ))
            """,
            (now, mission_id, hazard_id),
        )
        return cur.rowcount or 0


def hex_cell_id_at(mission_id: int, lat: float, lon: float) -> int | None:
    """Point-in-polygon lookup. Returns hex_cells.id or None."""
    with session() as conn:
        row = conn.execute(
            """
            SELECT id FROM hex_cells
            WHERE mission_id = ?
              AND ST_Contains(geom, MakePoint(?, ?, 4326))
            LIMIT 1
            """,
            (mission_id, lon, lat),
        ).fetchone()
        return row["id"] if row else None


def set_flag_clue_for_hex(hex_id: int) -> None:
    """Sets flag_clue=1 and updates flags_updated_ts."""
    now = int(time.time())
    with session() as conn:
        conn.execute(
            "UPDATE hex_cells SET flag_clue = 1, flags_updated_ts = ? WHERE id = ?",
            (now, hex_id),
        )


def mark_hex_searched(hex_id: int, user_id: int, ts: int) -> None:
    """Mark a hex cell as covered by a searcher's ping. Idempotent at the
    cell level — repeated calls update searched_by_user_id and searched_ts
    (last-writer-wins) but flag_searched stays set.
    """
    with session() as conn:
        conn.execute(
            """
            UPDATE hex_cells
            SET flag_searched = 1,
                searched_by_user_id = ?,
                searched_ts = ?
            WHERE id = ?
            """,
            (user_id, ts, hex_id),
        )
