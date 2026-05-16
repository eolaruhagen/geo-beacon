"""Per-volunteer routing payloads for the dispatch agent.

The routing agent does not receive a whole-mission brief. It receives one
small local map for one searcher, picks a local (col, row), and the worker
translates that local choice back to a world hex id.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import time
from typing import Any

from api.db import session
from agent.skills.read import _haversine_m


VIEW_SIZE = 10
CENTER_COL = 5
CENTER_ROW = 5


@dataclass(frozen=True)
class DispatchContext:
    """Payload plus the hidden local-to-world mapping for one LLM call."""

    mission_id: int
    user_id: int
    callsign: str
    text: str
    local_to_hex: dict[tuple[int, int], int]
    cells_by_local: dict[tuple[int, int], dict[str, Any]]
    recommended_target: tuple[int, int] | None

    def hex_id_for(self, col: int, row: int) -> int:
        key = (col, row)
        if key not in self.local_to_hex:
            raise ValueError(f"Local target ({col}, {row}) is outside this dispatch view")
        return self.local_to_hex[key]

    def is_dispatchable(self, col: int, row: int) -> bool:
        cell = self.cells_by_local.get((col, row))
        if cell is None:
            return False
        return _is_traversable(cell) and not bool(cell["flag_searched"])


def build_dispatch_payload(mission_id: int, user_id: int) -> str:
    """Return only the user-message text for the per-searcher routing agent."""
    return build_dispatch_context(mission_id, user_id).text


def build_dispatch_context(mission_id: int, user_id: int) -> DispatchContext:
    """Build the routing-agent payload and hidden coordinate mapping."""
    with session() as conn:
        user = conn.execute(
            """
            SELECT id, display_name, callsign, role, status
            FROM users
            WHERE id = ? AND current_mission_id = ?
            """,
            (user_id, mission_id),
        ).fetchone()
        if user is None:
            raise ValueError(f"User {user_id} is not in mission {mission_id}")
        if user["role"] != "searcher":
            raise ValueError(f"User {user_id} is role={user['role']!r}; only searchers are routed")

        ping = conn.execute(
            """
            SELECT ts, lat, lon, accuracy_m, battery_pct
            FROM pings
            WHERE mission_id = ? AND user_id = ?
            ORDER BY ts DESC LIMIT 1
            """,
            (mission_id, user_id),
        ).fetchone()
        if ping is None:
            raise ValueError(f"User {user_id} has no GPS pings in mission {mission_id}")

        mission = conn.execute(
            """
            SELECT id, name, pls_lat, pls_lon, pls_ts
            FROM missions
            WHERE id = ?
            """,
            (mission_id,),
        ).fetchone()
        if mission is None:
            raise ValueError(f"Mission {mission_id} not found")

        cells = _load_cells(conn, mission_id)
        if not cells:
            raise ValueError(f"Mission {mission_id} has no hex cells")

        latest_by_user = _latest_pings_by_user(conn, mission_id)
        latest_findings = _latest_finding_by_hex(conn, mission_id)

    _attach_grid_indexes(cells)
    cells_by_id = {int(cell["id"]): cell for cell in cells}
    cells_by_grid = {
        (int(cell["grid_col"]), int(cell["grid_row"])): cell
        for cell in cells
    }

    center_cell = _nearest_cell(cells, float(ping["lat"]), float(ping["lon"]))
    center_grid = (int(center_cell["grid_col"]), int(center_cell["grid_row"]))

    view_cells: dict[tuple[int, int], dict[str, Any]] = {}
    local_to_hex: dict[tuple[int, int], int] = {}
    for row in range(VIEW_SIZE):
        for col in range(VIEW_SIZE):
            grid_col = center_grid[0] + (col - CENTER_COL)
            grid_row = center_grid[1] + (CENTER_ROW - row)
            cell = cells_by_grid.get((grid_col, grid_row))
            if cell is None:
                continue
            key = (col, row)
            view_cells[key] = cell
            local_to_hex[key] = int(cell["id"])

    other_positions = _local_other_volunteers(
        user_id=user_id,
        latest_by_user=latest_by_user,
        cells=cells,
        cells_by_id=cells_by_id,
        local_to_hex=local_to_hex,
    )
    pls_cell = _nearest_cell(cells, float(mission["pls_lat"]), float(mission["pls_lon"]))
    pls_local = _local_for_hex_id(local_to_hex, int(pls_cell["id"]))
    clue_positions = {
        key for key, cell in view_cells.items()
        if bool(cell["flag_clue"])
    }

    map_rows = _render_map(
        view_cells=view_cells,
        other_positions=other_positions,
        pls_local=pls_local,
        clue_positions=clue_positions,
    )
    cluster = _largest_unsearched_cluster(view_cells)
    recommended = _cluster_target(cluster)
    facts = _build_facts(
        volunteer_lat=float(ping["lat"]),
        volunteer_lon=float(ping["lon"]),
        mission=dict(mission),
        pls_local=pls_local,
        cells=cells,
        latest_findings=latest_findings,
        view_cells=view_cells,
        cluster=cluster,
        other_positions=other_positions,
    )
    callsign = user["callsign"] or user["display_name"] or f"User {user_id}"
    text = _format_payload(callsign, map_rows, facts)
    return DispatchContext(
        mission_id=mission_id,
        user_id=user_id,
        callsign=str(callsign),
        text=text,
        local_to_hex=local_to_hex,
        cells_by_local=view_cells,
        recommended_target=recommended,
    )


def _load_cells(conn, mission_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, mission_id, segment_id,
               center_elev_m, slope_deg, dominant_cover,
               has_trail, has_road, is_building, is_water,
               flag_danger, flag_impassable, flag_clue, flag_poi,
               flags_updated_ts, flag_searched, searched_by_user_id, searched_ts,
               X(Centroid(geom)) AS lon,
               Y(Centroid(geom)) AS lat
        FROM hex_cells
        WHERE mission_id = ?
        """,
        (mission_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _latest_pings_by_user(conn, mission_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT u.id AS user_id, u.callsign, u.display_name,
               p.ts, p.lat, p.lon
        FROM users u
        JOIN pings p ON p.id = (
          SELECT id FROM pings
          WHERE mission_id = ? AND user_id = u.id
          ORDER BY ts DESC LIMIT 1
        )
        WHERE u.current_mission_id = ?
          AND u.role = 'searcher'
        """,
        (mission_id, mission_id),
    ).fetchall()
    return {int(row["user_id"]): dict(row) for row in rows}


def _latest_finding_by_hex(conn, mission_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT f.hex_id, f.ts, f.kind, f.description, f.confidence
        FROM findings f
        JOIN (
          SELECT hex_id, MAX(ts) AS max_ts
          FROM findings
          WHERE mission_id = ?
          GROUP BY hex_id
        ) latest ON latest.hex_id = f.hex_id AND latest.max_ts = f.ts
        WHERE f.mission_id = ?
        """,
        (mission_id, mission_id),
    ).fetchall()
    return {int(row["hex_id"]): dict(row) for row in rows}


def _attach_grid_indexes(cells: list[dict[str, Any]]) -> None:
    """Infer stable grid column/row indexes from hex centroids."""
    columns: list[list[dict[str, Any]]] = []
    for cell in sorted(cells, key=lambda c: (float(c["lon"]), float(c["lat"]))):
        if not columns or abs(float(cell["lon"]) - float(columns[-1][0]["lon"])) > 1e-7:
            columns.append([cell])
        else:
            columns[-1].append(cell)

    for col_idx, column in enumerate(columns):
        for row_idx, cell in enumerate(sorted(column, key=lambda c: float(c["lat"]))):
            cell["grid_col"] = col_idx
            cell["grid_row"] = row_idx


def _nearest_cell(cells: list[dict[str, Any]], lat: float, lon: float) -> dict[str, Any]:
    return min(cells, key=lambda cell: _haversine_m(lat, lon, float(cell["lat"]), float(cell["lon"])))


def _local_for_hex_id(local_to_hex: dict[tuple[int, int], int], hex_id: int) -> tuple[int, int] | None:
    for key, local_hex_id in local_to_hex.items():
        if local_hex_id == hex_id:
            return key
    return None


def _local_other_volunteers(
    *,
    user_id: int,
    latest_by_user: dict[int, dict[str, Any]],
    cells: list[dict[str, Any]],
    cells_by_id: dict[int, dict[str, Any]],
    local_to_hex: dict[tuple[int, int], int],
) -> dict[tuple[int, int], str]:
    positions: dict[tuple[int, int], str] = {}
    for other_id, ping in latest_by_user.items():
        if other_id == user_id:
            continue
        cell = _nearest_cell(cells, float(ping["lat"]), float(ping["lon"]))
        key = _local_for_hex_id(local_to_hex, int(cell["id"]))
        if key is None:
            continue
        callsign = ping.get("callsign") or ping.get("display_name") or f"User {other_id}"
        positions[key] = str(callsign)

    # Keep cells_by_id referenced so callers notice if local_to_hex contains
    # ids not present in the loaded mission cells during future refactors.
    for hex_id in local_to_hex.values():
        if hex_id not in cells_by_id:
            raise ValueError(f"Local view references missing hex {hex_id}")
    return positions


def _render_map(
    *,
    view_cells: dict[tuple[int, int], dict[str, Any]],
    other_positions: dict[tuple[int, int], str],
    pls_local: tuple[int, int] | None,
    clue_positions: set[tuple[int, int]],
) -> list[str]:
    rows: list[str] = ["     0 1 2 3 4 5 6 7 8 9"]
    for row in range(VIEW_SIZE):
        symbols: list[str] = []
        for col in range(VIEW_SIZE):
            key = (col, row)
            cell = view_cells.get(key)
            if key == (CENTER_COL, CENTER_ROW):
                symbols.append("@")
            elif key in other_positions:
                symbols.append("v")
            elif pls_local == key:
                symbols.append("P")
            elif key in clue_positions:
                symbols.append("!")
            elif cell is None:
                symbols.append("#")
            elif not _is_traversable(cell):
                symbols.append("#")
            elif bool(cell["flag_searched"]):
                symbols.append("o")
            else:
                symbols.append(".")
        rows.append(f"   {row} " + " ".join(symbols))
    return rows


def _is_traversable(cell: dict[str, Any]) -> bool:
    return not (
        bool(cell["flag_impassable"])
        or bool(cell["is_water"])
        or bool(cell["is_building"])
    )


def _largest_unsearched_cluster(
    view_cells: dict[tuple[int, int], dict[str, Any]],
) -> list[tuple[int, int]]:
    eligible = {
        key for key, cell in view_cells.items()
        if _is_traversable(cell) and not bool(cell["flag_searched"])
    }
    seen: set[tuple[int, int]] = set()
    clusters: list[list[tuple[int, int]]] = []
    for key in sorted(eligible):
        if key in seen:
            continue
        cluster: list[tuple[int, int]] = []
        queue: deque[tuple[int, int]] = deque([key])
        seen.add(key)
        while queue:
            cur = queue.popleft()
            cluster.append(cur)
            col, row = cur
            for nxt in ((col - 1, row), (col + 1, row), (col, row - 1), (col, row + 1)):
                if nxt in eligible and nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        clusters.append(cluster)
    if not clusters:
        return []
    return max(clusters, key=len)


def _cluster_target(cluster: list[tuple[int, int]]) -> tuple[int, int] | None:
    if not cluster:
        return None
    avg_col = sum(col for col, _row in cluster) / len(cluster)
    avg_row = sum(row for _col, row in cluster) / len(cluster)
    return min(cluster, key=lambda key: (key[0] - avg_col) ** 2 + (key[1] - avg_row) ** 2)


def _build_facts(
    *,
    volunteer_lat: float,
    volunteer_lon: float,
    mission: dict[str, Any],
    pls_local: tuple[int, int] | None,
    cells: list[dict[str, Any]],
    latest_findings: dict[int, dict[str, Any]],
    view_cells: dict[tuple[int, int], dict[str, Any]],
    cluster: list[tuple[int, int]],
    other_positions: dict[tuple[int, int], str],
) -> list[str]:
    facts: list[str] = []
    if pls_local is None:
        dist = _haversine_m(volunteer_lat, volunteer_lon, float(mission["pls_lat"]), float(mission["pls_lon"]))
        facts.append(
            "Subject's last known position: "
            f"{round(dist)} m {_bearing_label(volunteer_lat, volunteer_lon, float(mission['pls_lat']), float(mission['pls_lon']))}"
        )

    clue_cells = [cell for cell in cells if bool(cell["flag_clue"])]
    if clue_cells:
        clue_cell = min(
            clue_cells,
            key=lambda cell: _haversine_m(volunteer_lat, volunteer_lon, float(cell["lat"]), float(cell["lon"])),
        )
        clue_local = _local_for_hex_id(
            {key: int(cell["id"]) for key, cell in view_cells.items()},
            int(clue_cell["id"]),
        )
        dist = _haversine_m(volunteer_lat, volunteer_lon, float(clue_cell["lat"]), float(clue_cell["lon"]))
        when = _age_text(int(clue_cell["flags_updated_ts"] or time.time()))
        where = f"({clue_local[0]}, {clue_local[1]})" if clue_local else f"{round(dist)} m"
        finding = latest_findings.get(int(clue_cell["id"]))
        kind = finding["kind"] if finding else "clue"
        facts.append(
            f"Nearest clue/finding: {where} - {round(dist)} m "
            f"{_bearing_label(volunteer_lat, volunteer_lon, float(clue_cell['lat']), float(clue_cell['lon']))}, "
            f"{kind}, reported {when}"
        )

    if cluster:
        target = _cluster_target(cluster)
        assert target is not None
        facts.append(
            "Largest unsearched cluster in view: "
            f"{len(cluster)} cells, centered at ({target[0]}, {target[1]}) - "
            f"{_local_bearing_label(CENTER_COL, CENTER_ROW, target[0], target[1])}"
        )

    impassable = sorted(
        key for key, cell in view_cells.items()
        if not _is_traversable(cell)
    )
    if impassable:
        facts.append(f"Impassable area: {_describe_cells(impassable)}")

    if other_positions:
        parts = [f"{name} at ({col}, {row})" for (col, row), name in sorted(other_positions.items())]
        facts.append("Other volunteers in view: " + ", ".join(parts))
    else:
        facts.append("Other volunteers in view: none")
    return facts


def _format_payload(callsign: str, map_rows: list[str], facts: list[str]) -> str:
    lines = [
        f"Volunteer: {callsign}",
        "",
        "Map:",
        *map_rows,
        "",
        "Facts:",
    ]
    lines.extend(f"- {fact}" for fact in facts)
    return "\n".join(lines).strip()


def _bearing_label(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = (
        math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
        - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1))
    )
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return labels[int((bearing + 22.5) // 45) % 8]


def _local_bearing_label(col1: int, row1: int, col2: int, row2: int) -> str:
    dcol = col2 - col1
    drow = row1 - row2
    if dcol == 0 and drow == 0:
        return "here"
    angle = (math.degrees(math.atan2(dcol, drow)) + 360.0) % 360.0
    labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return labels[int((angle + 22.5) // 45) % 8]


def _age_text(ts: int) -> str:
    age_s = max(0, int(time.time()) - ts)
    if age_s < 90:
        return "just now"
    return f"{round(age_s / 60)} min ago"


def _describe_cells(cells: list[tuple[int, int]]) -> str:
    rows = [row for _col, row in cells]
    cols = [col for col, _row in cells]
    row_text = _range_text(rows, "row", "rows")
    col_text = _range_text(cols, "column", "columns")
    return f"{row_text}, {col_text}"


def _range_text(values: list[int], singular: str, plural: str) -> str:
    lo = min(values)
    hi = max(values)
    if lo == hi:
        return f"{singular} {lo}"
    return f"{plural} {lo}-{hi}"
