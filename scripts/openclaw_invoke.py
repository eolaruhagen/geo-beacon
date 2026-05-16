#!/usr/bin/env python3
"""Bridge between workers/agent.py and OpenClaw inside the docker sandbox.

workers/agent.py expects OPENCLAW_ROUTER_COMMAND (or OPENCLAW_COMMAND) to
be a binary that reads the prompt from stdin. The OpenClaw CLI itself
takes the prompt as a `-m` flag and lives inside the docker sandbox, so
a small bridge is needed.

This wrapper:
  1. Finds the OpenClaw sandbox container by name.
  2. Reads the prompt from its own stdin (piped in by agent.py).
  3. Shells the prompt into `openclaw agent` inside the container, with
     the session-id workers/agent.py set in GB_OPENCLAW_SESSION_ID so
     parallel per-searcher calls don't collide on a shared session.

Set OPENCLAW_ROUTER_COMMAND=/abs/path/to/this/file and you're done.

Env honored:
  GEO_BEACON_SANDBOX       sandbox container name match (default: my-assistant)
  GB_OPENCLAW_SESSION_ID   session id (set per-call by workers/agent.py)
"""
from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    sandbox = os.environ.get("GEO_BEACON_SANDBOX", "my-assistant")
    session_id = os.environ.get("GB_OPENCLAW_SESSION_ID", "geo-beacon")

    cid = subprocess.check_output(
        ["docker", "ps", "-qf", f"name={sandbox}"]
    ).decode().strip()
    if not cid:
        print(f"[openclaw_invoke] no running container named '{sandbox}'", file=sys.stderr)
        return 2

    brief = sys.stdin.read()

    inner = (
        ". /tmp/nemoclaw-proxy-env.sh && "
        "HOME=/sandbox "
        "openclaw agent --agent main --json "
        f"--session-id {session_id} "
        '-m "$(cat)"'
    )
    cmd = ["docker", "exec", "-i", "-u", "sandbox", cid, "sh", "-lc", inner]
    result = subprocess.run(cmd, input=brief, text=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
