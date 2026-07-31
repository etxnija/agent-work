"""
loop.py — Phase 1 harness loop: plan → approve → implement (per task)

Usage (via CLI after pip install -e):
    agent loop "add a /health endpoint"
    agent loop   # prompts interactively

Environment:
    AGENT_TOOL     driver to use (default: claude)
    AGENT_MODE     approval gate to use (default: interactive)
    AGENT_SANDBOX  sandbox backend to use (default: worktree)
"""

import hashlib
import re
import sys
from datetime import date
from pathlib import Path

from .drivers import get_driver
from .gates import get_gate
from .sandbox import get_sandbox

PLAN_FILE = "plan.md"
AGENTS_MD = "AGENTS.md"
STATUS_MD = "memory/status.md"

PLANNER_AGENT = "planner"
PLAN_READY_SIGNAL = "PLAN READY"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _file_hash(path: str) -> str | None:
    """MD5 of a file's contents, or None if the file doesn't exist."""
    p = Path(path)
    return hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else None


def _append_status(entry: str) -> None:
    """Append an entry to status.md if it exists."""
    p = Path(STATUS_MD)
    if p.exists():
        p.write_text(p.read_text() + "\n" + entry)


def _parse_tasks(plan_path: str) -> list[str]:
    """
    Extract the numbered task list from plan.md's ## Tasks section.

    Each task is everything from 'N. ' up to (but not including) the next
    numbered item or the next ## heading. Returns an empty list if no Tasks
    section or no numbered items are found.
    """
    content = Path(plan_path).read_text()

    section_match = re.search(
        r'^## Tasks\s*\n(.*?)(?=^##|\Z)', content, re.MULTILINE | re.DOTALL
    )
    if not section_match:
        return []

    section = section_match.group(1)
    positions = [m.start() for m in re.finditer(r'^\d+\.\s+', section, re.MULTILINE)]
    if not positions:
        return []

    tasks = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(section)
        tasks.append(section[pos:end].strip())
    return tasks


def _task_title(task_text: str) -> str:
    """First line of a task with leading 'N. ' stripped, truncated for display."""
    first_line = task_text.split('\n')[0].strip()
    return re.sub(r'^\d+\.\s+', '', first_line)[:80]


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_loop(task: str) -> int:
    """
    Execute one plan → approve → implement cycle, implementing one task at a time.
    Returns exit code (0 = success, 1 = error, 2 = rejected by human).
    """
    driver = get_driver()
    gate = get_gate()
    sandbox = get_sandbox()

    project_root = Path.cwd().resolve()
    plan_abs = str(project_root / PLAN_FILE)
    agents_abs = str(project_root / AGENTS_MD)
    status_abs = str(project_root / STATUS_MD)

    # ── 1. Plan ──────────────────────────────────────────────────────────────
    # Planner runs in the project root (read-only; no sandbox needed).
    print(f"\n[planner] Task: {task!r}")
    print("[planner] Exploring codebase…")

    plan_result = driver.run_subagent(
        PLANNER_AGENT,
        f"Task: {task}\n\nExplore the codebase and write a plan to plan.md following your instructions.",
    )

    if plan_result.exit_code != 0:
        print(f"[error] Planner exited with code {plan_result.exit_code}:\n{plan_result.text}")
        return 1

    plan_path = Path(PLAN_FILE)
    if not plan_path.exists():
        print(f"[error] Planner did not write {PLAN_FILE}.\nPlanner output:\n{plan_result.text}")
        return 1

    if PLAN_READY_SIGNAL not in plan_result.text:
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

    # ── 3. Parse tasks ────────────────────────────────────────────────────────
    tasks = _parse_tasks(PLAN_FILE)
    if not tasks:
        print(
            f"[error] No numbered tasks found in {PLAN_FILE}. "
            "The plan must have a '## Tasks' section with items like '1. **title** — …'"
        )
        return 1

    print(f"\n[loop] {len(tasks)} task(s) to implement.")

    # ── 4. Implement task by task (inside sandbox) ────────────────────────────
    with sandbox.workspace(project_root) as handle:
        for i, task_text in enumerate(tasks, 1):
            print(f"\n[worker] Task {i}/{len(tasks)}: {_task_title(task_text)}")

            status_hash_before = _file_hash(status_abs)

            worker_prompt = (
                f"Implement this specific task from the approved plan in {plan_abs}:\n\n"
                f"{task_text}\n\n"
                f"Implement only this task — do not work ahead to other tasks.\n"
                f"Follow all conventions in {agents_abs}.\n"
                f"After completing, append a one-line summary of what you did to {status_abs} "
                f"under today's date ({date.today().isoformat()})."
            )
            worker_result = driver.run(
                worker_prompt,
                context_files=[plan_abs, agents_abs, status_abs],
                cwd=handle.path,
            )

            if worker_result.exit_code != 0:
                print(f"[error] Worker failed on task {i}/{len(tasks)}:\n{worker_result.text}")
                print(f"[loop] Stopped at task {i}. Completed: {i - 1}/{len(tasks)}.")
                return 1  # handle._keep is False → sandbox discards the branch

            if _file_hash(status_abs) == status_hash_before:
                print(f"[warning] Worker did not update {STATUS_MD} after task {i}.")

            # Phase 2: run sensors here (lint, test, coverage)
            # Phase 3: git commit per task here

        handle.keep()  # all tasks complete → preserve the branch for Phase 3 merge

    print(f"\n[loop] All {len(tasks)} tasks complete.")
    return 0


# Entry point is cli.py:main — run via `agent loop [task]`
