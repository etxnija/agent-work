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
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .drivers import get_driver
from .drivers.base import AgentDriver, AgentResult
from .gates import get_gate
from .sandbox import get_sandbox

PLAN_FILE = "plan.md"
AGENTS_MD = "AGENTS.md"
STATUS_MD = "memory/status.md"

PLANNER_AGENT = "planner"
PLAN_READY_SIGNAL = "PLAN READY"
PLANNER_RETRY_LIMIT = 2
SENSOR_RETRY_LIMIT = 2

REVIEWER_AGENT = "reviewer"
REVIEW_APPROVED_SIGNAL = "REVIEW: APPROVED"
REVIEW_CHANGES_SIGNAL = "REVIEW: CHANGES REQUESTED"
REVIEW_RETRY_LIMIT = 2


# ── Metrics ───────────────────────────────────────────────────────────────────

@dataclass
class Metrics:
    calls: int = 0
    cost_usd: float = 0.0
    last_session_id: str | None = None

    def record(self, result: AgentResult) -> None:
        self.calls += 1
        if result.cost_usd is not None:
            self.cost_usd += result.cost_usd
        if result.session_id is not None:
            self.last_session_id = result.session_id


class _MeteredDriver(AgentDriver):
    """Wraps an AgentDriver, recording every call into a shared Metrics instance."""

    def __init__(self, inner: AgentDriver, metrics: Metrics) -> None:
        self._inner = inner
        self._metrics = metrics

    def run(
        self,
        prompt: str,
        context_files: list[str] | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
        result = self._inner.run(prompt, context_files=context_files, cwd=cwd)
        self._metrics.record(result)
        return result

    def run_subagent(self, agent_name: str, prompt: str, cwd: Path | None = None) -> AgentResult:
        result = self._inner.run_subagent(agent_name, prompt, cwd=cwd)
        self._metrics.record(result)
        return result


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


def _plan_invalid_reason(plan_text: str, plan_path: str) -> str | None:
    """
    Name what's wrong with a planner-produced plan, or None if it's usable.

    Covers only the two retryable conditions: a missing sign-off line and an
    unparseable Tasks section. A crashed subprocess or an entirely-missing
    plan.md are handled separately in run_loop() and are out of scope here.
    """
    if PLAN_READY_SIGNAL not in plan_text:
        return f'missing the "{PLAN_READY_SIGNAL} — awaiting approval." line'
    if not _parse_tasks(plan_path):
        return "no parseable numbered ## Tasks section"
    return None


def _task_title(task_text: str) -> str:
    """First line of a task with leading 'N. ' stripped, truncated for display."""
    first_line = task_text.split('\n')[0].strip()
    return re.sub(r'^\d+\.\s+', '', first_line)[:80]


def _main_checkout_dirty_paths(project_root: Path, status_abs: str) -> list[str]:
    """
    Return paths (relative to project_root) with uncommitted changes in the main
    checkout, excluding status_abs — the one file workers intentionally write
    there directly via absolute path, by design (see AGENTS.md).

    A worker runs with cwd set to its sandboxed worktree; anything else showing
    up dirty here means it wrote outside that sandbox.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root, capture_output=True, text=True, check=False,
    )
    status_rel = str(Path(status_abs).resolve().relative_to(project_root))
    paths = []
    for line in result.stdout.splitlines():
        path = line[3:].strip()
        if " -> " in path:  # rename: "old -> new"
            path = path.split(" -> ", 1)[1]
        if path != status_rel:
            paths.append(path)
    return paths


def _run_sensors(cwd: Path) -> list[tuple[str, str]]:
    """
    Run sensors/*.sh scripts in cwd, in sorted order, stopping at the first
    failure.

    Returns a single-element list [(script_name, combined_output)] for the
    first sensor that exits non-zero, or an empty list if all sensors pass
    (or there is no sensors/ directory at all). Later sensors are not run
    once one has failed.
    """
    for script in sorted((cwd / "sensors").glob("*.sh")):
        result = subprocess.run(
            ["sh", str(script)], cwd=cwd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            return [(script.name, result.stdout + result.stderr)]
    return []


def _run_sensors_with_retry(
    worktree: Path,
    i: int,
    total: int,
    plan_abs: str,
    agents_abs: str,
    status_abs: str,
    driver: AgentDriver,
) -> tuple[list[tuple[str, str]], int]:
    """
    Run sensors, retrying up to SENSOR_RETRY_LIMIT times with a corrective
    worker call in between. Returns (failures, attempt) — failures is the
    (possibly still non-empty) failures list, now at most one entry since
    _run_sensors stops at the first failing sensor per pass (does not decide
    fail-closed itself, that's the caller's job); attempt is the number of
    retries used, 0 when sensors passed on the first try.
    """
    failures = _run_sensors(worktree)
    attempt = 0
    while failures and attempt < SENSOR_RETRY_LIMIT:
        attempt += 1
        print(
            f"[sensor] Task {i}/{total}: "
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
            cwd=worktree,
        )
        if corrective_result.exit_code != 0:
            print(
                f"[error] Corrective worker call failed on task {i}/{total}:\n"
                f"{corrective_result.text}"
            )
            break

        failures = _run_sensors(worktree)

    return failures, attempt


def _run_review_with_retry(
    worktree: Path,
    task_text: str,
    i: int,
    total: int,
    plan_abs: str,
    agents_abs: str,
    status_abs: str,
    driver: AgentDriver,
    review_critiques: dict[int, str],
) -> tuple[bool, str, int, int, list[tuple[str, str]]]:
    """
    Run the adversarial-review cycle for one task, retrying up to
    REVIEW_RETRY_LIMIT times with a corrective worker call in between.

    Mutates review_critiques in place exactly as the inline loop it replaces
    did: popped on approval, set on budget-exhausted or a failed corrective
    call, left untouched on a sensor regression mid-cycle. Does not decide
    fail-closed itself — a non-empty failures list is the only fail-closed
    signal, same contract as _run_sensors_with_retry; the caller must check it.

    Returns (approved, critique, review_attempt, sensor_retry_count, failures).
    """
    review_attempt = 0
    sensor_retry_count = 0
    while True:
        diff = _task_diff(worktree)
        review_prompt = (
            f"Review this task's diff against the task description and against "
            f"this repo's AGENTS.md conventions.\n\n"
            f"Task:\n{task_text}\n\n"
            f"Diff:\n```diff\n{diff}\n```"
        )
        review_result = driver.run_subagent(REVIEWER_AGENT, review_prompt, cwd=worktree)
        approved, critique = _review_verdict(review_result.text)

        if approved:
            review_critiques.pop(i, None)
            return approved, critique, review_attempt, sensor_retry_count, []

        if review_attempt >= REVIEW_RETRY_LIMIT:
            print(
                f"[review] Task {i}/{total}: review budget exhausted "
                f"({REVIEW_RETRY_LIMIT}/{REVIEW_RETRY_LIMIT}) — committing with "
                f"outstanding critique."
            )
            review_critiques[i] = critique
            return approved, critique, review_attempt, sensor_retry_count, []

        review_attempt += 1
        print(
            f"[review] Task {i}/{total}: changes requested "
            f"(attempt {review_attempt}/{REVIEW_RETRY_LIMIT})."
        )
        corrective_prompt = (
            f"A reviewer requested changes to your last change for this task:\n\n"
            f"{critique}\n\n"
            f"Fix them and nothing else."
        )
        corrective_result = driver.run(
            corrective_prompt,
            context_files=[plan_abs, agents_abs, status_abs],
            cwd=worktree,
        )
        if corrective_result.exit_code != 0:
            print(
                f"[error] Corrective worker call failed on task {i}/{total}:\n"
                f"{corrective_result.text}"
            )
            review_critiques[i] = critique
            return approved, critique, review_attempt, sensor_retry_count, []

        failures, attempt = _run_sensors_with_retry(
            worktree, i, total, plan_abs, agents_abs, status_abs, driver
        )
        sensor_retry_count += attempt
        if failures:
            print(
                f"[error] Sensors still failing on task {i}/{total}: "
                f"{', '.join(name for name, _ in failures)}."
            )
            return approved, critique, review_attempt, sensor_retry_count, failures


def _task_diff(worktree: Path) -> str:
    """
    Capture the worktree's current uncommitted changes as a diff string.

    Stages everything first (harmless pre-staging — _commit_task does its
    own git add -A right before committing) so new/modified/deleted files
    all show up, since sub-agents like the reviewer have no Bash and can't
    run git diff themselves.
    """
    subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True, check=False)
    result = subprocess.run(
        ["git", "diff", "--cached", "HEAD"],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    return result.stdout


def _review_verdict(text: str) -> tuple[bool, str]:
    """
    Parse the reviewer's marker-line output into (approved, critique).

    Neither marker present is treated as changes-requested (fail toward
    review, never silently approve on ambiguous output).
    """
    if REVIEW_APPROVED_SIGNAL in text:
        return True, text.split(REVIEW_APPROVED_SIGNAL, 1)[1].strip()
    if REVIEW_CHANGES_SIGNAL in text:
        return False, text.split(REVIEW_CHANGES_SIGNAL, 1)[1].strip()
    return False, text.strip()


def _worker_summary(text: str) -> str:
    """
    Extract the worker's one-line SUMMARY: from its response text.

    Absence just yields "" — unlike the PLAN_READY/REVIEW_* markers, this
    never gates the loop.
    """
    if "SUMMARY:" not in text:
        return ""
    remainder = text.rsplit("SUMMARY:", 1)[1].strip()
    return remainder.splitlines()[0] if remainder else ""


def _build_narrative(task: str, task_narratives: list[dict]) -> str:
    """
    Assemble the run narrative markdown from per-task one-liners.

    Pure formatting — no file I/O, no outcome section (appended separately
    once the merge decision is known).
    """
    lines = [f"# Run narrative: {task}"]
    for entry in task_narratives:
        lines.append("")
        lines.append(f"## Task {entry['num']}: {entry['title']}")
        summary = entry["summary"] or "(worker did not provide a summary)"
        lines.append(f"Summary: {summary}")
        if entry["review_approved"]:
            lines.append(f"Review: APPROVED — {entry['review_reasoning']}")
        else:
            lines.append(
                f"Review: CHANGES REQUESTED (unresolved) — {entry['review_reasoning']}"
            )
        sensor_retries = entry["sensor_retries"]
        review_retries = entry["review_retries"]
        if sensor_retries or review_retries:
            parts = []
            if sensor_retries:
                noun = "retry" if sensor_retries == 1 else "retries"
                parts.append(f"{sensor_retries} sensor {noun}")
            if review_retries:
                noun = "round" if review_retries == 1 else "rounds"
                parts.append(f"{review_retries} review {noun}")
            lines.append(f"Retries: {', '.join(parts)}")
    return "\n".join(lines) + "\n"


def _write_narrative(project_root: Path, run_timestamp: str, content: str) -> Path:
    """Write the assembled narrative markdown to logs/run-{run_timestamp}.md."""
    logs_dir = project_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    path = logs_dir / f"run-{run_timestamp}.md"
    path.write_text(content)
    return path


def _append_narrative_outcome(path: Path, outcome: str) -> None:
    """Append the final merge outcome to an already-written narrative file."""
    with path.open("a") as f:
        f.write(f"\n## Outcome\n{outcome}\n")


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


def _zellij_edit(path: str) -> None:
    """Open path in a floating Zellij pane using $EDITOR."""
    subprocess.run(
        ["zellij", "action", "edit", "--floating", "--near-current-pane", path],
        capture_output=True,
        check=False,
    )


def _show_diff_in_editor(branch: str, project_root: Path, critiques: dict[int, str] | None = None) -> None:
    """
    Open the branch's full diff against HEAD in a floating editor pane, if
    running inside Zellij. A no-op everywhere else — this is a personal
    workflow convenience, not something the harness depends on.
    """
    if "ZELLIJ" not in os.environ:
        return

    diff = subprocess.run(
        ["git", "diff", f"HEAD..{branch}"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if not diff.stdout.strip():
        return

    content = diff.stdout
    if critiques:
        lines = ["# Outstanding review critiques", ""]
        for task_num, critique in critiques.items():
            lines.append(f"## Task {task_num}")
            lines.append(critique)
            lines.append("")
        content = "\n".join(lines) + "\n" + content

    fd, path = tempfile.mkstemp(prefix="agent-diff-", suffix=".diff")
    with os.fdopen(fd, "w") as f:
        f.write(content)

    _zellij_edit(path)


def _offer_merge(
    branch: str,
    project_root: Path,
    task: str = "",
    critiques: dict[int, str] | None = None,
    narrative_path: Path | None = None,
) -> str:
    """
    Show commits on branch and offer a squash-merge into HEAD.

    Squash merge: all task commits on the branch are collapsed into one commit
    on main, keeping the main history linear. The branch retains its per-task
    commits for traceability until it is deleted.

    Returns a short outcome string describing what happened.
    """
    commits = _branch_commits(branch, project_root)

    if not commits:
        print(f"\n[merge] Branch '{branch}' has no commits ahead of HEAD.")
        print("[merge] The worker may not have committed. Branch preserved for manual inspection.")
        print(f"[merge]   git log HEAD..{branch}")
        return "no commits"

    print(f"\n[merge] Branch '{branch}' — {len(commits)} commit(s) to squash into main:")
    for c in commits:
        print(f"  {c}")

    if critiques:
        print("\n[review] Outstanding critiques from unresolved review cycles:")
        for task_num, critique in critiques.items():
            print(f"  Task {task_num}: {critique}")

    _show_diff_in_editor(branch, project_root, critiques)
    if narrative_path is not None and "ZELLIJ" in os.environ:
        _zellij_edit(str(narrative_path))

    answer = input("\n[merge] Squash-merge into current branch? (y/n) ").strip().lower()
    if answer != "y":
        print(f"[merge] Branch '{branch}' preserved. To squash-merge manually:")
        print(f"  git merge --squash {branch} && git commit && git branch -D {branch}")
        return "declined"

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
        return "squash failed"

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
        return "merged"

    print(f"[merge] Commit failed: {commit.stderr.strip()}")
    print(f"[merge] Branch '{branch}' preserved.")
    return "commit failed"


def _update_coverage_baseline(project_root: Path) -> None:
    """
    Refresh .coverage-baseline from a fresh run of the project's own
    sensors/test.sh against main.

    Language-agnostic: delegates entirely to whatever sensors/test.sh
    the project's bootstrapped language preset put in place, and only
    looks for a coverage.json report it may have produced. Best-effort
    — a failure here, or the absence of a coverage-producing sensor,
    must not undo an already-successful merge, so it warns/skips and
    leaves the existing baseline (if any) untouched rather than raising.
    """
    test_sh = project_root / "sensors" / "test.sh"
    coverage_json = project_root / "coverage.json"

    if not test_sh.exists():
        print("[coverage] Baseline update skipped: no sensors/test.sh in this project.")
        return

    subprocess.run(
        ["sh", str(test_sh)], cwd=project_root, capture_output=True, text=True, check=False
    )

    if not coverage_json.exists():
        print("[coverage] Baseline update skipped: sensors/test.sh produced no coverage report.")
        return

    percent_covered = json.loads(coverage_json.read_text())["totals"]["percent_covered"]
    (project_root / ".coverage-baseline").write_text(str(percent_covered))

    coverage_json.unlink(missing_ok=True)
    (project_root / ".coverage").unlink(missing_ok=True)

    subprocess.run(
        ["git", "add", ".coverage-baseline"], cwd=project_root, capture_output=True, check=False
    )
    subprocess.run(
        ["git", "commit", "-m", "Update coverage baseline"],
        cwd=project_root,
        capture_output=True,
        check=False,
    )


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_loop(task: str) -> int:
    """
    Execute one plan → approve → implement cycle, implementing one task at a time.
    Returns exit code (0 = success, 1 = error, 2 = rejected by human).
    """
    driver = get_driver()
    run_metrics = Metrics()
    driver = _MeteredDriver(driver, run_metrics)
    gate = get_gate()
    sandbox = get_sandbox()

    project_root = Path.cwd().resolve()
    plan_abs = str(project_root / PLAN_FILE)
    agents_abs = str(project_root / AGENTS_MD)
    status_abs = str(project_root / STATUS_MD)
    run_timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")

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

    attempt = 0
    invalid_reason = _plan_invalid_reason(plan_result.text, PLAN_FILE)
    while invalid_reason is not None and attempt < PLANNER_RETRY_LIMIT:
        attempt += 1
        print(f"[planner] Plan invalid (attempt {attempt}/{PLANNER_RETRY_LIMIT}): {invalid_reason}")

        corrective_prompt = (
            f"Task: {task}\n\n"
            f"Your previous plan.md was invalid: {invalid_reason}\n\n"
            f"Rewrite plan.md. It must end with the exact line "
            f'"{PLAN_READY_SIGNAL} — awaiting approval." and must contain a '
            "'## Tasks' section with a numbered list of tasks (e.g. '1. **title** — …')."
        )
        plan_result = driver.run_subagent(PLANNER_AGENT, corrective_prompt)

        if plan_result.exit_code != 0:
            print(f"[error] Planner exited with code {plan_result.exit_code}:\n{plan_result.text}")
            return 1

        if not plan_path.exists():
            print(f"[error] Planner did not write {PLAN_FILE}.\nPlanner output:\n{plan_result.text}")
            return 1

        invalid_reason = _plan_invalid_reason(plan_result.text, PLAN_FILE)

    if invalid_reason is not None:
        print(
            f"[error] Planner still produced an invalid plan after "
            f"{PLANNER_RETRY_LIMIT} retries: {invalid_reason}"
        )
        return 1

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
    review_critiques: dict[int, str] = {}
    task_narratives: list[dict] = []

    with sandbox.workspace(project_root) as handle:
        for i, task_text in enumerate(tasks, 1):
            print(f"\n[worker] Task {i}/{len(tasks)}: {_task_title(task_text)}")

            calls_before, cost_before = run_metrics.calls, run_metrics.cost_usd
            status_hash_before = _file_hash(status_abs)
            main_dirty_before = _main_checkout_dirty_paths(project_root, status_abs)

            worker_prompt = (
                f"Implement this specific task from the approved plan in {plan_abs}:\n\n"
                f"{task_text}\n\n"
                f"Implement only this task — do not work ahead to other tasks.\n"
                f"Follow all conventions in {agents_abs}.\n"
                f"After completing, append a one-line summary of what you did to {status_abs} "
                f"under today's date ({datetime.now(tz=UTC).date().isoformat()}).\n"
                f"End your response with a line starting with 'SUMMARY: ' followed by one "
                f"sentence on what changed and why."
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

            worker_summary = _worker_summary(worker_result.text)
            sensor_retry_count = 0

            if _file_hash(status_abs) == status_hash_before:
                print(f"[warning] Worker did not update {STATUS_MD} after task {i}.")

            failures, attempt = _run_sensors_with_retry(
                handle.path, i, len(tasks), plan_abs, agents_abs, status_abs, driver
            )
            sensor_retry_count += attempt

            if failures:
                print(
                    f"[error] Sensors still failing on task {i}/{len(tasks)}: "
                    f"{', '.join(name for name, _ in failures)}."
                )
                print(f"[loop] Stopped at task {i}. Completed: {i - 1}/{len(tasks)}.")
                handle.keep()
                if handle.branch:
                    print(
                        f"[loop] Branch '{handle.branch}' preserved — {i - 1} completed "
                        f"task(s) are not lost. Inspect with `git log {handle.branch} --oneline`, "
                        f"or merge manually with `git merge --squash {handle.branch} && git commit`."
                    )
                return 1  # handle.keep() called above → branch preserved for manual recovery

            # ── Adversarial review ──────────────────────────────────────────
            approved, critique, review_attempt, review_sensor_retry_count, failures = (
                _run_review_with_retry(
                    handle.path,
                    task_text,
                    i,
                    len(tasks),
                    plan_abs,
                    agents_abs,
                    status_abs,
                    driver,
                    review_critiques,
                )
            )
            sensor_retry_count += review_sensor_retry_count

            if failures:
                print(
                    f"[error] Sensors still failing on task {i}/{len(tasks)}: "
                    f"{', '.join(name for name, _ in failures)}."
                )
                print(f"[loop] Stopped at task {i}. Completed: {i - 1}/{len(tasks)}.")
                handle.keep()
                if handle.branch:
                    print(
                        f"[loop] Branch '{handle.branch}' preserved — {i - 1} completed "
                        f"task(s) are not lost. Inspect with `git log {handle.branch} --oneline`, "
                        f"or merge manually with `git merge --squash {handle.branch} && git commit`."
                    )
                return 1  # handle.keep() called above → branch preserved for manual recovery

            task_narratives.append(
                {
                    "num": i,
                    "title": _task_title(task_text),
                    "summary": worker_summary,
                    "review_approved": approved,
                    "review_reasoning": critique,
                    "sensor_retries": sensor_retry_count,
                    "review_retries": review_attempt,
                }
            )

            main_dirty_after = _main_checkout_dirty_paths(project_root, status_abs)
            leaked = sorted(set(main_dirty_after) - set(main_dirty_before))
            if leaked:
                print(
                    f"[warning] Task {i}/{len(tasks)}: the worker wrote outside its "
                    f"sandboxed worktree, into the main checkout: {', '.join(leaked)}. "
                    f"This should not happen (see AGENTS.md sandboxing gotchas) — "
                    f"inspect and resolve with `git status`/`git diff` before merging."
                )

            _commit_task(i, _task_title(task_text), handle.path)

            task_calls = run_metrics.calls - calls_before
            task_cost = run_metrics.cost_usd - cost_before
            print(
                f"[metrics] Task {i}/{len(tasks)}: {task_calls} driver call(s), "
                f"${task_cost:.4f}, session {run_metrics.last_session_id}"
            )

        handle.keep()  # all tasks complete → preserve the branch for merge

    narrative_content = _build_narrative(task, task_narratives)
    narrative_path = _write_narrative(project_root, run_timestamp, narrative_content)

    if handle.branch:  # "" when NoopSandbox is active (tests / no-git projects)
        outcome = _offer_merge(
            handle.branch, project_root, task, review_critiques, narrative_path=narrative_path
        )
        _append_narrative_outcome(narrative_path, outcome)
        if outcome == "merged":
            _update_coverage_baseline(project_root)

    print(
        f"[metrics] Run total: {run_metrics.calls} driver call(s), "
        f"${run_metrics.cost_usd:.4f}, session {run_metrics.last_session_id}"
    )
    _append_status(
        f"\n**Run metrics:** {run_metrics.calls} driver call(s), "
        f"${run_metrics.cost_usd:.4f}, session {run_metrics.last_session_id}\n"
    )

    print(f"\n[loop] All {len(tasks)} tasks complete.")
    return 0


# Entry point is cli.py:main — run via `agent loop [task]`
