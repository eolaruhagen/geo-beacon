"""Snap-to-trail route hint helper.

This is the cheap "no-graph-routing" snap that spec §13 calls out: given a
point, find the closest point on any mission trail via SpatiaLite
ClosestPoint. The agent skill `query_route` uses the same primitive.

We don't do real path-along-trail routing for the hackathon. The output is
two snap points (one near the start, one near the target); the app draws
straight lines through them. Good enough for "walk to this trail, follow
it that way, leave it here."
"""
from __future__ import annotations

import json

from api.db import session


def snap_point_to_nearest_trail(
    mission_id: int, lat: float, lon: float,
) -> tuple[float, float] | None:
    """Return (lat, lon) of the closest point on any mission trail to the
    input point, or None if the mission has no trail features.

    Uses SpatiaLite `ClosestPoint(line, point)` which returns the projected
    point on the line nearest to the input. Coordinates are degrees; we
    take the trail with the smallest planar distance, which is fine for
    sub-km hops where Earth curvature doesn't matter.
    """
    with session() as conn:
        row = conn.execute(
            """
            SELECT AsGeoJSON(ClosestPoint(geom, MakePoint(?, ?, 4326))) AS pt,
                   Distance(geom, MakePoint(?, ?, 4326)) AS d
            FROM osm_features
            WHERE mission_id = ? AND kind = 'trail'
            ORDER BY d ASC
            LIMIT 1
            """,
            (lon, lat, lon, lat, mission_id),
        ).fetchone()
    if row is None or row["pt"] is None:
        return None
    pt = json.loads(row["pt"])
    # GeoJSON Point coordinates are [lon, lat]
    snap_lon, snap_lat = pt["coordinates"]
    return (snap_lat, snap_lon)
