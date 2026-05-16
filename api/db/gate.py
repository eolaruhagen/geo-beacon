from __future__ import annotations

import json
import time

from api.db import session


def enqueue_trigger(mission_id: int, trigger: str, context: dict | None = None) -> int:
    """Inserts agent_invocation_queue row. Returns id. Worker not implemented yet."""
    now = int(time.time())
    with session() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_invocation_queue (mission_id, trigger, context, created_ts)
            VALUES (?, ?, ?, ?)
            """,
            (mission_id, trigger, json.dumps(context) if context is not None else None, now),
        )
        return cur.lastrowid
