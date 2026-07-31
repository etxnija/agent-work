"""
loop.py — Phase 1 harness loop: plan → approve → implement

Usage (via CLI after pip install -e):
    agent loop "add a /health endpoint"
    agent loop   # prompts interactively

Environment:
    AGENT_TOOL   driver to use (default: claude)
    AGENT_MODE   approval gate to use (default: interactive)
"""

import hashlib
import sys
from datetime import date
from pathlib import Path

from .drivers import get_driver
from .gates import get_gate

PLAN_FILE = "plan.md"
AGENTS_MD = "AGENTS.md"
STATUS_MD = "memory/status.md"

PLANNER_AGENT = "planner"
PLAN_READY_SIGNAL = "PLAN READY"


def _file_hash(path: str) -> str | None:
    """MD5 of a file's contents, or None if the file doesn't exist."""
    p = Path(path)
    return hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else None


def _append_status(entry: str) -> None:
    """Append a timestamped entry to status.md if it exists."""
    p = Path(STATUS_MD)
    if p.exists():
        p.write_text(p.read_text() + "\n" + entry)


def run_loop(task: str) -> int:
    """
    Execute one plan → approve → implement cycle.
    Returns exit code (0 = success, 1 = error, 2 = rejected by human).
    """
    driver = get_driver()
    gate = get_gate()

    # ── 1. Plan ──────────────────────────────────────────────────────────────
    print(f"\n[planner] Task: {task!r}")
    print("[planner] Exploring codebase…")

    planner_prompt = (
        f"Task: {task}\n\n"
        "Explore the codebase and write a plan to plan.md following your instructions."
    )
    plan_result = driver.run_subagent(PLANNER_AGENT, planner_prompt)

    if plan_result.exit_code != 0:
        print(f"[error] Planner exited with code {plan_result.exit_code}:\n{plan_result.text}")
        return 1

    plan_path = Path(PLAN_FILE)
    if not plan_path.exists():
        print(f"[error] Planner did not write {PLAN_FILE}.\nPlanner output:\n{plan_result.text}")
        return 1

    if PLAN_READY_SIGNAL not in plan_result.text:
        # Plan file exists but the signal is missing — surface the output and
        # continue; the human can decide whether the plan is usable.
        print(f"[warning] Planner output did not contain '{PLAN_READY_SIGNAL}'.")
        print(f"[warning] Planner output:\n{plan_result.text}\n")

    # ── 2. Approval gate ─────────────────────────────────────────────────────
    approved = gate.request(PLAN_FILE)
    if not approved:
        print("[loop] Plan rejected — exiting without changes.")
        _append_status(
            f"\n## {date.today().isoformat()} — plan rejected\n"
            f"Task: {task}\n"
            f"Plan written but not approved by human.\n"
        )
        return 2

    # ── 3. Implement ─────────────────────────────────────────────────────────
    print("\n[worker] Implementing approved plan…")
    status_hash_before = _file_hash(STATUS_MD)

    worker_prompt = (
        f"Implement the plan in {PLAN_FILE} exactly as described.\n"
        f"Follow all conventions in {AGENTS_MD}.\n"
        f"After completing, append a brief summary of what you did to {STATUS_MD} "
        f"under today's date ({date.today().isoformat()})."
    )
    worker_result = driver.run(
        worker_prompt,
        context_files=[PLAN_FILE, AGENTS_MD, STATUS_MD],
    )

    if worker_result.exit_code != 0:
        print(f"[error] Worker exited with code {worker_result.exit_code}:\n{worker_result.text}")
        return 1

    if _file_hash(STATUS_MD) == status_hash_before:
        print(f"[warning] Worker did not update {STATUS_MD} — work log may be incomplete.")

    print("\n[loop] Done.")
    return 0


# Entry point is cli.py:main — run via `agent loop [task]`
