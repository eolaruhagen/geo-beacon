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
    """Mark a hex cell as covered by a searcher's ping.

    **First-writer-wins**: once a cell is `flag_searched = 1`, this is a
    no-op. Attribution (`searched_by_user_id`, `searched_ts`) is
    frozen at the first searcher whose ping landed inside.

    This rule keeps per-searcher coverage tints stable: two searchers
    crossing paths don't flicker each other's territory on the map.
    The "who got there first" data is also more useful than a
    last-writer-wins overwrite — for SAR coverage analysis, first
    contact is the canonical event.
    """
    with session() as conn:
        conn.execute(
            """
            UPDATE hex_cells
            SET flag_searched = 1,
                searched_by_user_id = ?,
                searched_ts = ?
            WHERE id = ? AND flag_searched = 0
            """,
            (user_id, ts, hex_id),
        )


def mark_segment_searched(
    mission_id: int, segment_id: int, user_id: int, ts: int,
) -> int:
    """Mark every UNSEARCHED hex cell in `segment_id` as searched by `user_id`.

    Called from POST /field/dispatch/{id}/complete: when a searcher
    closes out a dispatch the system fills in coverage for cells they
    didn't physically walk through.

    Same first-writer-wins rule as `mark_hex_searched`: cells already
    `flag_searched = 1` (whether by an earlier ping or an earlier
    completed dispatch from someone else) are left untouched.
    Attribution is sticky once set.

    Returns the count of cells newly flagged on this call.
    """
    with session() as conn:
        cur = conn.execute(
            """
            UPDATE hex_cells
            SET flag_searched = 1,
                searched_by_user_id = ?,
                searched_ts = ?
            WHERE mission_id = ? AND segment_id = ? AND flag_searched = 0
            """,
            (user_id, ts, mission_id, segment_id),
        )
        return cur.rowcount or 0
