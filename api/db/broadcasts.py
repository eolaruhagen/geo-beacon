"""DB helpers for the broadcasts table.

# Visibility policy (RLS-like — enforced in the helper, NOT the DB)

`broadcasts.scope` is either:
  - `'all'`               — visible to every searcher in the mission
  - `'user:{user_id}'`    — visible only to that one user

SQLite has no row-level-security primitive, so we encode the policy here:
every read goes through `visible_broadcasts_for_user(user_id, mission_id, …)`
which filters on `mission_id = ?` AND `(scope = 'all' OR scope = ?)`.

**Do not query `broadcasts` directly from a route handler.** Always route
through this module so the scope check can't be accidentally bypassed.
If a future writer adds a new scope keyword, update this docstring and the
filter in the same change.
"""
from __future__ import annotations

import time

from api.db import session

_VISIBLE_COLS = "id, mission_id, scope, kind, message, ts"


def _user_scope(user_id: int) -> str:
    return f"user:{user_id}"


def visible_broadcasts_for_user(
    user_id: int,
    mission_id: int,
    since_ts: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """All broadcasts visible to (user, mission), newest-first.

    `since_ts`: if provided, only broadcasts with `ts > since_ts` are returned.
    `limit`:    if provided, caps the row count. Always ordered ts DESC, so
                this returns the most recent N (which is what the
                inline `/field/me` poll wants).
    """
    sql = (
        f"SELECT {_VISIBLE_COLS} FROM broadcasts "
        "WHERE mission_id = ? AND (scope = 'all' OR scope = ?)"
    )
    params: list = [mission_id, _user_scope(user_id)]
    if since_ts is not None:
        sql += " AND ts > ?"
        params.append(since_ts)
    sql += " ORDER BY ts DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with session() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def insert_broadcast(
    mission_id: int,
    scope: str,
    kind: str,
    message: str,
    ts: int | None = None,
) -> int:
    """Insert a broadcast row. `scope` must be `'all'` or `f'user:{id}'`;
    callers are expected to construct it correctly (use `user_scope()` for
    targeted broadcasts).

    Used by the agent skill `broadcast()` and indirectly by `dispatch_searcher`
    / `recall_searcher` / `flag_hazard`. The /field tier doesn't write.
    """
    if ts is None:
        ts = int(time.time())
    with session() as conn:
        cur = conn.execute(
            "INSERT INTO broadcasts (mission_id, scope, kind, message, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (mission_id, scope, kind, message, ts),
        )
        return cur.lastrowid


def user_scope(user_id: int) -> str:
    """Build the `scope` string for a single-user broadcast.

    Always use this when writing — never hand-format `f'user:{id}'` so a
    rename here propagates to every caller.
    """
    return _user_scope(user_id)
