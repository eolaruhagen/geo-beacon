"""MCP server exposing geo-beacon agent skills.

Run on the DGX from the repo root:

    python -m agent.mcp_server

OpenClaw can connect to this process over stdio. Trimmed down after the
§10 Mission Brief loop and commander-grade skills were deprecated. The
routing agent runs in-process (see docs/2026-05-16-dispatch-agent.md) and
does not use MCP; what's exposed here is a thin debug surface only.
"""
from __future__ import annotations

import os
import sys

from agent.skills import read, write


SERVER_NAME = "geo-beacon-sar"
SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}


def _register_tools(mcp) -> None:
    # Read tools (debug surface).
    mcp.tool()(read.get_searcher)
    mcp.tool()(read.get_findings)
    mcp.tool()(read.recent_events)

    # Write tools.
    mcp.tool()(write.dispatch_searcher)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


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

    transport = os.getenv("GEO_BEACON_MCP_TRANSPORT", "stdio").strip() or "stdio"
    if transport not in SUPPORTED_TRANSPORTS:
        print(
            "GEO_BEACON_MCP_TRANSPORT must be one of "
            f"{sorted(SUPPORTED_TRANSPORTS)}, got {transport!r}.",
            file=sys.stderr,
        )
        return 2

    host = os.getenv("GEO_BEACON_MCP_HOST", "127.0.0.1")
    port = _env_int("GEO_BEACON_MCP_PORT", 8000)

    # Host/port are ignored for stdio but matter when OpenClaw runs in a
    # sandbox and reaches this server over streamable HTTP.
    mcp = FastMCP(SERVER_NAME, host=host, port=port)
    _register_tools(mcp)
    mcp.run(transport=transport)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
