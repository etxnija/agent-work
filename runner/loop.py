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
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .drivers import get_driver
from .gates import get_gate
from .sandbox import get_sandbox

PLAN_FILE = "plan.md"
AGENTS_MD = "AGENTS.md"
STATUS_MD = "memory/status.md"

PLANNER_AGENT = "planner"
PLAN_READY_SIGNAL = "PLAN READY"
SENSOR_RETRY_LIMIT = 2


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


def _run_sensors(cwd: Path) -> list[tuple[str, str]]:
    """
    Run every sensors/*.sh script in cwd, in sorted order.

    Returns a list of (script_name, combined_output) for each sensor that
    exited non-zero. Empty list means all sensors passed, or there is no
    sensors/ directory at all.
    """
    failures = []
    for script in sorted((cwd / "sensors").glob("*.sh")):
        result = subprocess.run(
            ["sh", str(script)], cwd=cwd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            failures.append((script.name, result.stdout + result.stderr))
    return failures


# ── Git helpers ──────────────────────────────────────────────────────────────

def _commit_task(task_num: int, title: str, worktree: Path) -> None:
    """
    Stage all changes in the worktree and commit them with a task-scoped message.
    Called after each worker task succeeds (Phase 3 commit hook).
    Skips silently if there is nothing to commit (e.g. worker only updated
    status.md, which is written to the project root via absolute path).
    """
    add = subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True, check=False)
    if add.returncode != 0:
        return

    commit = subprocess.run(
        ["git", "commit", "-m", f"Task {task_num}: {title}"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode == 0:
        sha = commit.stdout.strip().splitlines()[0] if commit.stdout else ""
        print(f"[commit] Task {task_num} committed ({sha}).")
    elif "nothing to commit" in (commit.stdout + commit.stderr):
        print(f"[commit] Task {task_num}: nothing new to commit in worktree.")
    else:
        print(f"[commit] Warning: commit failed — {commit.stderr.strip()}")


# ── Merge helper ─────────────────────────────────────────────────────────────

def _branch_commits(branch: str, project_root: Path) -> list[str]:
    """Return one-line summaries of commits on branch that are not on HEAD."""
    result = subprocess.run(
        ["git", "log", f"HEAD..{branch}", "--oneline"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return [l for l in result.stdout.splitlines() if l.strip()]


def _offer_merge(branch: str, project_root: Path, task: str = "") -> None:
    """
    Show commits on branch and offer a squash-merge into HEAD.

    Squash merge: all task commits on the branch are collapsed into one commit
    on main, keeping the main history linear. The branch retains its per-task
    commits for traceability until it is deleted.
    """
    commits = _branch_commits(branch, project_root)

    if not commits:
        print(f"\n[merge] Branch '{branch}' has no commits ahead of HEAD.")
        print("[merge] The worker may not have committed. Branch preserved for manual inspection.")
        print(f"[merge]   git log HEAD..{branch}")
        return

    print(f"\n[merge] Branch '{branch}' — {len(commits)} commit(s) to squash into main:")
    for c in commits:
        print(f"  {c}")

    answer = input("\n[merge] Squash-merge into current branch? (y/n) ").strip().lower()
    if answer != "y":
        print(f"[merge] Branch '{branch}' preserved. To squash-merge manually:")
        print(f"  git merge --squash {branch} && git commit && git branch -D {branch}")
        return

    squash = subprocess.run(
        ["git", "merge", "--squash", branch],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if squash.returncode != 0:
        print(f"[merge] Squash failed: {squash.stderr.strip()}")
        print(f"[merge] Branch '{branch}' preserved.")
        return

    # Build a single commit message: task as subject, per-task commits as body.
    subject = task.strip() or branch
    body = "\n".join(f"- {c}" for c in commits)
    commit_msg = f"{subject}\n\n{body}"

    commit = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode == 0:
        subprocess.run(
            ["git", "branch", "-D", branch], cwd=project_root, capture_output=True, check=False
        )
        print(f"[merge] Done. Squashed into one commit; branch '{branch}' deleted.")
    else:
        print(f"[merge] Commit failed: {commit.stderr.strip()}")
        print(f"[merge] Branch '{branch}' preserved.")


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
            f"\n## {datetime.now(tz=UTC).date().isoformat()} — plan rejected\n"
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
                f"under today's date ({datetime.now(tz=UTC).date().isoformat()})."
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

            failures = _run_sensors(handle.path)
            attempt = 0
            while failures and attempt < SENSOR_RETRY_LIMIT:
                attempt += 1
                print(
                    f"[sensor] Task {i}/{len(tasks)}: "
                    f"{', '.join(name for name, _ in failures)} failed "
                    f"(attempt {attempt}/{SENSOR_RETRY_LIMIT})."
                )
                formatted_failures = "\n\n".join(
                    f"### {name}\n{output}" for name, output in failures
                )
                corrective_prompt = (
                    f"Your last change to this task produced these issues:\n\n"
                    f"{formatted_failures}\n\n"
                    f"Fix them and nothing else."
                )
                corrective_result = driver.run(
                    corrective_prompt,
                    context_files=[plan_abs, agents_abs, status_abs],
                    cwd=handle.path,
                )
                if corrective_result.exit_code != 0:
                    print(
                        f"[error] Corrective worker call failed on task {i}/{len(tasks)}:\n"
                        f"{corrective_result.text}"
                    )
                    break

                failures = _run_sensors(handle.path)

            if failures:
                print(
                    f"[error] Sensors still failing on task {i}/{len(tasks)} "
                    f"after {attempt} attempt(s): "
                    f"{', '.join(name for name, _ in failures)}."
                )
                print(f"[loop] Stopped at task {i}. Completed: {i - 1}/{len(tasks)}.")
                return 1  # handle._keep is False → sandbox discards the branch

            _commit_task(i, _task_title(task_text), handle.path)

        handle.keep()  # all tasks complete → preserve the branch for merge

    if handle.branch:  # "" when NoopSandbox is active (tests / no-git projects)
        _offer_merge(handle.branch, project_root, task)

    print(f"\n[loop] All {len(tasks)} tasks complete.")
    return 0


# Entry point is cli.py:main — run via `agent loop [task]`
