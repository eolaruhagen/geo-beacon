"""MCP server exposing geo-beacon agent skills.

Run on the DGX from the repo root:

    python -m agent.mcp_server

OpenClaw should connect to this process over stdio. The actual mission logic is
in `agent.skills.read` and `agent.skills.write`; this file is only the adapter.
"""
from __future__ import annotations

import sys

from agent.skills import read, write


SERVER_NAME = "geo-beacon-sar"


def _register_tools(mcp) -> None:
    # Read tools.
    mcp.tool()(read.get_mission_brief)
    mcp.tool()(read.get_mission_overview)
    mcp.tool()(read.get_segment)
    mcp.tool()(read.get_searcher)
    mcp.tool()(read.get_findings)
    mcp.tool()(read.get_terrain_summary)
    mcp.tool()(read.get_uncovered_areas)
    mcp.tool()(read.query_route)
    mcp.tool()(read.recent_events)

    # Write tools.
    mcp.tool()(write.dispatch_searcher)
    mcp.tool()(write.reassign_searcher)
    mcp.tool()(write.recall_searcher)
    mcp.tool()(write.broadcast)
    mcp.tool()(write.flag_hazard)
    mcp.tool()(write.update_segment_poa)
    mcp.tool()(write.update_mission_status)


def main() -> int:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print(
            "The Python MCP SDK is not installed. Install repo requirements "
            "or run `pip install mcp` in the DGX virtualenv.",
            file=sys.stderr,
        )
        return 1

    mcp = FastMCP(SERVER_NAME)
    _register_tools(mcp)
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

