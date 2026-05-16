"""Parallel per-searcher routing agent worker.

This replaces the old whole-mission brief loop. Each tick builds one local
ASCII payload per searcher, sends those payloads to the LLM in parallel, then
writes one cell-level dispatch per searcher.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any

from agent.payload import DispatchContext, build_dispatch_context
from agent.skills.read import active_missions, is_idle, list_searchers
from agent.skills.write import dispatch_to_cell
from scripts.apply_migrations import apply, DEFAULT_DB_PATH, DEFAULT_MIGRATIONS_DIR


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARALLELISM = 3

ROUTER_SYSTEM_PROMPT = """You dispatch one search-and-rescue volunteer per call.
The user message contains a 10x10 local view centered on that volunteer plus
pre-computed facts about their surroundings. Pick the best cell to send them to.

Map symbols:
  .  unsearched
  o  searched
  #  impassable
  P  subject's last known position
  !  clue or finding reported by a volunteer
  @  the volunteer you are dispatching, always at column 5, row 5
  v  another volunteer

Coordinates: column 0-9 left-to-right, row 0-9 top-to-bottom. North is up.

Prefer cells that:
1. Are reachable and not marked #
2. Are unsearched
3. Are near a recent clue/finding or the subject's last known position
4. Avoid obviously duplicating another volunteer's coverage

Return only JSON in this exact shape:
{"target_col": 0, "target_row": 0, "reasoning": "15 words or fewer"}

Do not include markdown, prose, or absolute coordinates."""


@dataclass
class RoutingDecision:
    target_col: int | None
    target_row: int | None
    reasoning: str
    source: str


@dataclass
class RoutingResult:
    mission_id: int
    user_id: int
    callsign: str
    status: str
    duration_s: float
    decision: dict[str, Any] | None = None
    dispatch: dict[str, Any] | None = None
    error: str | None = None
    model_text: str | None = None


def _command_from_env() -> list[str] | None:
    raw = (
        os.environ.get("OPENCLAW_ROUTER_COMMAND")
        or os.environ.get("ROUTING_AGENT_COMMAND")
        or os.environ.get("OPENCLAW_COMMAND")
        or ""
    ).strip()
    if not raw:
        return None
    return shlex.split(raw)


def _build_model_input(context: DispatchContext) -> str:
    return (
        f"{ROUTER_SYSTEM_PROMPT}\n\n"
        "----- ROUTING PAYLOAD -----\n"
        f"{context.text}\n"
        "----- END ROUTING PAYLOAD -----\n"
    )


def _extract_openclaw_text(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text

    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        result_payloads = result.get("payloads")
        if isinstance(result_payloads, list):
            pieces = [
                str(item.get("text", ""))
                for item in result_payloads
                if isinstance(item, dict) and item.get("text")
            ]
            if pieces:
                return "\n".join(pieces).strip()
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            if isinstance(result.get(key), str):
                return result[key].strip()
    return text


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("routing LLM response was not a JSON object")
    return obj


def _parse_decision(text: str) -> RoutingDecision:
    obj = _json_from_text(text)
    col = obj.get("target_col")
    row = obj.get("target_row")
    reasoning = str(obj.get("reasoning") or "Routing agent selected this local target.").strip()
    if col is None or row is None:
        return RoutingDecision(None, None, reasoning[:160], "llm")
    return RoutingDecision(int(col), int(row), reasoning[:160], "llm")


def _invoke_llm(context: DispatchContext, timeout_seconds: int | None) -> tuple[RoutingDecision, str]:
    command = _command_from_env()
    if command is None:
        raise RuntimeError(
            "No OpenClaw command configured. Set OPENCLAW_ROUTER_COMMAND "
            "or run with --mode heuristic."
        )

    env = os.environ.copy()
    env.setdefault("GB_OPENCLAW_THINKING", "off")
    if timeout_seconds is not None:
        env["GB_OPENCLAW_TIMEOUT"] = str(timeout_seconds)
    else:
        env.pop("GB_OPENCLAW_TIMEOUT", None)
    env["GB_OPENCLAW_SESSION_ID"] = (
        f"routing-{context.mission_id}-{context.user_id}-{int(time.time())}"
    )
    proc = subprocess.run(
        command,
        input=_build_model_input(context),
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=timeout_seconds,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"routing LLM exited {proc.returncode}: {(proc.stderr or proc.stdout)[-1200:]}"
        )
    model_text = _extract_openclaw_text(proc.stdout)
    return _parse_decision(model_text), model_text


def _heuristic_decision(context: DispatchContext) -> RoutingDecision:
    target = context.recommended_target
    if target is None:
        return RoutingDecision(None, None, "No unsearched traversable cell in local view.", "heuristic")
    col, row = target
    return RoutingDecision(col, row, "Largest nearby unsearched cluster.", "heuristic")


def _safe_decision(
    context: DispatchContext,
    decision: RoutingDecision,
    fallback_heuristic: bool,
) -> RoutingDecision:
    if (
        decision.target_col is not None
        and decision.target_row is not None
        and 0 <= decision.target_col < 10
        and 0 <= decision.target_row < 10
        and context.is_dispatchable(decision.target_col, decision.target_row)
    ):
        return decision
    if not fallback_heuristic:
        return decision
    fallback = _heuristic_decision(context)
    fallback.source = f"{decision.source}_fallback_heuristic"
    return fallback


def _dispatch_with_retry(
    *,
    user_id: int,
    target_hex_id: int,
    reasoning: str,
    instruction: str,
    mission_id: int,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            return dispatch_to_cell(
                user_id=user_id,
                target_hex_id=target_hex_id,
                reasoning=reasoning,
                instruction=instruction,
                mission_id=mission_id,
            )
        except Exception as exc:
            last_error = exc
            if "database is locked" not in str(exc).lower():
                raise
            time.sleep(0.15 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _route_one(
    context: DispatchContext,
    *,
    mode: str,
    timeout_seconds: int | None,
    dry_run: bool,
    fallback_heuristic: bool,
) -> RoutingResult:
    started = time.time()
    model_text: str | None = None
    try:
        if mode == "heuristic":
            decision = _heuristic_decision(context)
        else:
            decision, model_text = _invoke_llm(context, timeout_seconds)
        decision = _safe_decision(context, decision, fallback_heuristic)

        decision_dict = {
            "target_col": decision.target_col,
            "target_row": decision.target_row,
            "reasoning": decision.reasoning,
            "source": decision.source,
        }
        if decision.target_col is None or decision.target_row is None:
            return RoutingResult(
                mission_id=context.mission_id,
                user_id=context.user_id,
                callsign=context.callsign,
                status="no_action",
                duration_s=round(time.time() - started, 2),
                decision=decision_dict,
                model_text=model_text,
            )

        target_hex_id = context.hex_id_for(decision.target_col, decision.target_row)
        instruction = f"Move to local cell ({decision.target_col}, {decision.target_row})."
        dispatch = None
        if not dry_run:
            dispatch = _dispatch_with_retry(
                user_id=context.user_id,
                target_hex_id=target_hex_id,
                reasoning=decision.reasoning,
                instruction=instruction,
                mission_id=context.mission_id,
            )

        return RoutingResult(
            mission_id=context.mission_id,
            user_id=context.user_id,
            callsign=context.callsign,
            status="dispatched" if not dry_run else "dry_run",
            duration_s=round(time.time() - started, 2),
            decision={**decision_dict, "target_hex_id": target_hex_id},
            dispatch=dispatch,
            model_text=model_text,
        )
    except Exception as exc:
        return RoutingResult(
            mission_id=context.mission_id,
            user_id=context.user_id,
            callsign=context.callsign,
            status="error",
            duration_s=round(time.time() - started, 2),
            error=str(exc),
            model_text=model_text,
        )


def _contexts_for_tick(
    args: argparse.Namespace,
) -> tuple[list[DispatchContext], list[RoutingResult]]:
    missions = [{"id": args.mission_id}] if args.mission_id else active_missions()
    contexts: list[DispatchContext] = []
    skipped: list[RoutingResult] = []
    for mission in missions:
        mission_id = int(mission["id"])
        for searcher in list_searchers(mission_id):
            if searcher["role"] != "searcher":
                continue
            if args.user_id is not None and int(searcher["id"]) != args.user_id:
                continue
            if searcher["latest_ping"] is None:
                continue
            if args.skip_active and searcher["active_dispatch"] is not None:
                continue
            if args.skip_idle and is_idle(
                int(searcher["id"]),
                mission_id,
                window_s=args.idle_window_s,
                min_distance_m=args.idle_min_distance_m,
            ):
                skipped.append(
                    RoutingResult(
                        mission_id=mission_id,
                        user_id=int(searcher["id"]),
                        callsign=searcher.get("callsign") or str(searcher["id"]),
                        status="skipped_idle",
                        duration_s=0.0,
                    )
                )
                continue
            contexts.append(build_dispatch_context(mission_id, int(searcher["id"])))
            if args.max_searchers and len(contexts) >= args.max_searchers:
                return contexts, skipped
    return contexts, skipped


def run_tick(args: argparse.Namespace) -> list[RoutingResult]:
    apply(os.environ.get("MISSION_DB_PATH", DEFAULT_DB_PATH), DEFAULT_MIGRATIONS_DIR)
    contexts, skipped = _contexts_for_tick(args)
    if args.print_payloads:
        for context in contexts:
            print(f"===== {context.callsign} =====")
            print(context.text)
            print()
    if args.payloads_only:
        payload_results = [
            RoutingResult(
                mission_id=context.mission_id,
                user_id=context.user_id,
                callsign=context.callsign,
                status="payload_only",
                duration_s=0.0,
            )
            for context in contexts
        ]
        combined = payload_results + skipped
        combined.sort(key=lambda item: (item.mission_id, item.callsign, item.user_id))
        return combined
    if not contexts:
        skipped.sort(key=lambda item: (item.mission_id, item.callsign, item.user_id))
        return skipped

    max_workers = max(1, min(args.parallelism, len(contexts)))
    results: list[RoutingResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(
                _route_one,
                context,
                mode=args.mode,
                timeout_seconds=args.timeout_seconds,
                dry_run=args.dry_run,
                fallback_heuristic=args.fallback_heuristic,
            )
            for context in contexts
        ]
        for future in as_completed(futures):
            results.append(future.result())
    results.extend(skipped)
    results.sort(key=lambda item: (item.mission_id, item.callsign, item.user_id))
    return results


def _emit_results(results: list[RoutingResult]) -> None:
    for result in results:
        print(json.dumps(result.__dict__, sort_keys=True), flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-id", type=int, default=None)
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--max-searchers", type=int, default=0)
    parser.add_argument("--parallelism", type=int, default=int(os.environ.get("ROUTING_AGENT_PARALLELISM", DEFAULT_PARALLELISM)))
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
        help="Optional per-searcher LLM timeout. Defaults to no timeout.",
    )
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--skip-active", action="store_true", help="Do not re-route users who already have an active dispatch")
    parser.add_argument(
        "--skip-idle",
        dest="skip_idle",
        action="store_true",
        default=True,
        help="Skip volunteers who have not moved recently (default: on)",
    )
    parser.add_argument(
        "--no-skip-idle",
        dest="skip_idle",
        action="store_false",
        help="Disable idle gating; route every volunteer with a latest ping",
    )
    parser.add_argument(
        "--idle-window-s",
        type=int,
        default=int(os.environ.get("ROUTING_AGENT_IDLE_WINDOW_S", 120)),
        help="Movement window (seconds) used by the idle predicate",
    )
    parser.add_argument(
        "--idle-min-distance-m",
        type=float,
        default=float(os.environ.get("ROUTING_AGENT_IDLE_MIN_DISTANCE_M", 10.0)),
        help="First-to-last displacement (meters) below which a searcher is idle",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run model/heuristic but do not write dispatch rows")
    parser.add_argument("--payloads-only", action="store_true", help="Build payloads and exit without model calls")
    parser.add_argument("--print-payloads", action="store_true")
    parser.add_argument("--no-fallback-heuristic", dest="fallback_heuristic", action="store_false")
    parser.set_defaults(fallback_heuristic=True)
    parser.add_argument(
        "--mode",
        choices=("llm", "heuristic"),
        default=os.environ.get("ROUTING_AGENT_MODE", "llm"),
        help="llm uses OPENCLAW_ROUTER_COMMAND; heuristic is local and useful for smoke tests",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    while True:
        started = time.time()
        results = run_tick(args)
        _emit_results(results)
        if not args.loop:
            return 1 if any(result.status == "error" for result in results) else 0
        elapsed = time.time() - started
        time.sleep(max(0.0, args.interval_seconds - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
