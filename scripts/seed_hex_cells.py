#!/usr/bin/env python3
"""Assign hex cells to segments and bulk-insert into hex_cells table.

Usage:
    python scripts/seed_hex_cells.py --mission-id N [--verbose]

For each hex in hex_data, performs a point-in-polygon lookup against the
inserted segments (via SpatiaLite ST_Contains), then bulk-inserts hex_cells.
Hexes that fall outside any segment are dropped.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import session
from api.db.hex_cells import bulk_insert_hex_cells

log = logging.getLogger("seed_hex_cells")


def seed_hex_cells(
    mission_id: int,
    hex_data: list[dict],
    segment_ids: list[int],
) -> int:
    """Assign each hex to a segment, insert hex_cells.

    Returns count of hex_cells inserted. Hexes outside any segment are dropped.

    Algorithm: segments are a regular 100m grid, so we can build an in-memory
    (row, col) → segment_id lookup by reading every segment's bbox in a single
    SQL call, then compute (row, col) for each hex via integer arithmetic.
    Avoids ~27k SQL round-trips that the naive per-hex ST_Contains needed.
    """
    if not hex_data or not segment_ids:
        log.warning("No hex_data or segment_ids — nothing to insert")
        return 0

    with session() as conn:
        seg_rows = conn.execute(
            """
            SELECT id,
                   MbrMinX(geom) AS minx, MbrMinY(geom) AS miny,
                   MbrMaxX(geom) AS maxx, MbrMaxY(geom) AS maxy
            FROM segments WHERE mission_id = ?
            """,
            (mission_id,),
        ).fetchall()

    if not seg_rows:
        log.warning("No segments found for mission %d", mission_id)
        return 0

    # Segment grid step — every segment in seed_segments has the same width/height.
    s0 = seg_rows[0]
    dlon = float(s0["maxx"]) - float(s0["minx"])
    dlat = float(s0["maxy"]) - float(s0["miny"])
    if dlon <= 0 or dlat <= 0:
        log.warning("Degenerate segment bbox dlon=%s dlat=%s; falling back to slow path", dlon, dlat)
        return _seed_hex_cells_slow(mission_id, hex_data)

    grid_origin_lon = min(float(r["minx"]) for r in seg_rows)
    grid_origin_lat = min(float(r["miny"]) for r in seg_rows)

    grid: dict[tuple[int, int], int] = {}
    for r in seg_rows:
        col = round((float(r["minx"]) - grid_origin_lon) / dlon)
        row = round((float(r["miny"]) - grid_origin_lat) / dlat)
        grid[(row, col)] = int(r["id"])

    rows: list[dict] = []
    dropped = 0
    for h in hex_data:
        col = int((h["center_lon"] - grid_origin_lon) / dlon)
        row = int((h["center_lat"] - grid_origin_lat) / dlat)
        seg_id = grid.get((row, col))
        if seg_id is None:
            dropped += 1
            continue
        rows.append({
            "poly_geojson": h["poly_geojson"],
            "segment_id": seg_id,
            "center_elev_m": h["center_elev_m"],
            "slope_deg": h["slope_deg"],
            "dominant_cover": h["dominant_cover"],
            "has_trail": h.get("has_trail", False),
            "has_road": h.get("has_road", False),
            "is_building": h.get("is_building", False),
            "is_water": h.get("is_water", False),
        })

    if dropped:
        log.info("Dropped %d hexes outside any segment", dropped)
    if not rows:
        log.warning("No hex rows to insert after segment assignment")
        return 0

    n = bulk_insert_hex_cells(mission_id, rows)
    log.info("Inserted %d hex_cells for mission %d", n, mission_id)
    return n


def _seed_hex_cells_slow(mission_id: int, hex_data: list[dict]) -> int:
    """Fallback: ST_Contains per hex. Only used if the segment grid lookup
    degenerates (shouldn't happen with a regular grid)."""
    rows: list[dict] = []
    dropped = 0
    with session() as conn:
        for h in hex_data:
            row = conn.execute(
                "SELECT id FROM segments WHERE mission_id = ? "
                "AND ST_Contains(geom, MakePoint(?, ?, 4326)) LIMIT 1",
                (mission_id, h["center_lon"], h["center_lat"]),
            ).fetchone()
            if row is None:
                dropped += 1
                continue
            rows.append({
                "poly_geojson": h["poly_geojson"],
                "segment_id": row["id"],
                "center_elev_m": h["center_elev_m"],
                "slope_deg": h["slope_deg"],
                "dominant_cover": h["dominant_cover"],
                "has_trail": h.get("has_trail", False),
                "has_road": h.get("has_road", False),
                "is_building": h.get("is_building", False),
                "is_water": h.get("is_water", False),
            })
    log.info("Slow path inserted=%d dropped=%d", len(rows), dropped)
    return bulk_insert_hex_cells(mission_id, rows) if rows else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mission-id", type=int, required=True)
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    print(f"[seed_hex_cells] mission_id={args.mission_id}", flush=True)
    print("[seed_hex_cells] CLI mode: fetching terrain + segments first", flush=True)
    try:
        from scripts.fetch_terrain import fetch_terrain
        from scripts.seed_segments import seed_segments

        result = fetch_terrain(args.mission_id, mock=True)
        hex_data = result["hex_data"]
        segment_ids = seed_segments(args.mission_id, hex_data)
        n = seed_hex_cells(args.mission_id, hex_data, segment_ids)
    except Exception as exc:
        print(f"[seed_hex_cells] ERROR: {exc}", file=sys.stderr, flush=True)
        return 1

    print(f"[seed_hex_cells] done: {n} hex_cells inserted", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
