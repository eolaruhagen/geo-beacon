from __future__ import annotations

from api.db import session


def insert_ping(
    user_id: int,
    mission_id: int,
    lat: float,
    lon: float,
    ts: int,
    accuracy_m: float | None = None,
    speed_mps: float | None = None,
    battery_pct: int | None = None,
    source: str = "phone",
) -> int:
    """Inserts row. geom = MakePoint(lon, lat, 4326). Returns ping_id."""
    with session() as conn:
        cur = conn.execute(
            """
            INSERT INTO pings (user_id, mission_id, ts, lat, lon,
                               accuracy_m, speed_mps, battery_pct, source, geom)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, MakePoint(?, ?, 4326))
            """,
            (user_id, mission_id, ts, lat, lon,
             accuracy_m, speed_mps, battery_pct, source,
             lon, lat),
        )
        return cur.lastrowid
