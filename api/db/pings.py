from __future__ import annotations

from api.db import session


def latest_ping_for_user(user_id: int, mission_id: int) -> dict | None:
    """Most recent ping for (user, mission), or None if the user has never pinged."""
    with session() as conn:
        row = conn.execute(
            """
            SELECT id, ts, lat, lon, accuracy_m, speed_mps, battery_pct, source
            FROM pings
            WHERE user_id = ? AND mission_id = ?
            ORDER BY ts DESC LIMIT 1
            """,
            (user_id, mission_id),
        ).fetchone()
        return dict(row) if row else None


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
