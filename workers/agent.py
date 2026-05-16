"""One-shot OpenClaw agent runner.

This script is intentionally small: it finds active missions, builds the brief,
loads the standing prompt, and invokes the external OpenClaw command if one is
configured. OpenClaw gets tools by connecting to `python -m agent.mcp_server`.

Cron/systemd can run this every minute on the DGX.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from agent.brief import compose_brief
from agent.skills.read import active_missions, recent_events
from scripts.apply_migrations import apply, DEFAULT_DB_PATH, DEFAULT_MIGRATIONS_DIR


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_PATH = REPO_ROOT / "openclaw" / "agent_prompt.md"
DEFAULT_EVENT_WINDOW_SECONDS = 90
DEFAULT_TIMEOUT_SECONDS = 180


def _load_prompt(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Agent prompt not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _build_model_input(prompt: str, brief: str) -> str:
    return (
        f"{prompt}\n\n"
        "----- CURRENT MISSION BRIEF -----\n"
        f"{brief.strip()}\n"
        "----- END BRIEF -----\n"
    )


def _should_invoke(mission_id: int, event_window_seconds: int, force: bool) -> bool:
    if force:
        return True
    since = int(time.time()) - event_window_seconds
    return len(recent_events(mission_id=mission_id, since_ts=since, limit=1)) > 0


def _invoke_openclaw(model_input: str, timeout_seconds: int) -> subprocess.CompletedProcess[str] | None:
    command = os.environ.get("OPENCLAW_COMMAND", "").strip()
    if not command:
        print(
            "[agent] OPENCLAW_COMMAND is not set; generated brief only. "
            "Set it to your OpenClaw CLI invocation when the DGX runtime is ready.",
            file=sys.stderr,
        )
        return None

    args = shlex.split(command)
    print(f"[agent] invoking: {' '.join(args)}", flush=True)
    return subprocess.run(
        args,
        input=model_input,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=timeout_seconds,
        check=False,
    )


def run_once(
    *,
    mission_id: int | None,
    dry_run: bool,
    force: bool,
    prompt_path: Path,
    event_window_seconds: int,
    timeout_seconds: int,
) -> int:
    apply(os.environ.get("MISSION_DB_PATH", DEFAULT_DB_PATH), DEFAULT_MIGRATIONS_DIR)
    prompt = _load_prompt(prompt_path)

    missions = [{"id": mission_id}] if mission_id is not None else active_missions()
    if not missions:
        print("[agent] no active missions", flush=True)
        return 0

    exit_code = 0
    for mission in missions:
        mid = int(mission["id"])
        if not _should_invoke(mid, event_window_seconds, force):
            print(f"[agent] mission {mid}: no recent events; skipping", flush=True)
            continue

        brief = compose_brief(mission_id=mid)
        model_input = _build_model_input(prompt, brief)
        if dry_run:
            print(model_input)
            continue

        try:
            result = _invoke_openclaw(model_input, timeout_seconds)
        except subprocess.TimeoutExpired:
            print(f"[agent] mission {mid}: OpenClaw command timed out", file=sys.stderr)
            exit_code = 1
            continue

        if result is None:
            print(brief)
            continue

        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode != 0:
            print(f"[agent] mission {mid}: command exited {result.returncode}", file=sys.stderr)
            exit_code = result.returncode

    return exit_code


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-id", type=int, default=None, help="Run only one mission")
    parser.add_argument("--dry-run", action="store_true", help="Print model input instead of invoking OpenClaw")
    parser.add_argument("--force", action="store_true", help="Run even if no recent events were found")
    parser.add_argument(
        "--prompt",
        type=Path,
        default=Path(os.environ.get("OPENCLAW_PROMPT_PATH", DEFAULT_PROMPT_PATH)),
        help="Path to standing agent prompt",
    )
    parser.add_argument(
        "--event-window-seconds",
        type=int,
        default=int(os.environ.get("AGENT_EVENT_WINDOW_SECONDS", DEFAULT_EVENT_WINDOW_SECONDS)),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.environ.get("OPENCLAW_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return run_once(
        mission_id=args.mission_id,
        dry_run=args.dry_run,
        force=args.force,
        prompt_path=args.prompt,
        event_window_seconds=args.event_window_seconds,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())

