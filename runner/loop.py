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
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .code_health import check_code_health
from .drivers import get_driver
from .drivers.base import AgentDriver
from .gates import get_gate
from .metrics import Metrics, _MeteredDriver
from .sandbox import get_sandbox

PLAN_FILE = "plan.md"
AGENTS_MD = "AGENTS.md"
STATUS_MD = "memory/status.md"

PLANNER_AGENT = "planner"
PLAN_READY_SIGNAL = "PLAN READY"
PLANNER_RETRY_LIMIT = 2
SENSOR_RETRY_LIMIT = 2
CODE_HEALTH_RETRY_LIMIT = 2

REVIEWER_AGENT = "reviewer"
REVIEW_APPROVED_SIGNAL = "REVIEW: APPROVED"
REVIEW_CHANGES_SIGNAL = "REVIEW: CHANGES REQUESTED"
REVIEW_RETRY_LIMIT = 2

WORKER_STATIC_INSTRUCTIONS = (
    "You are an autonomous software engineering worker implementing a single task.\n"
    "Follow these rules strictly:\n"
    "1. Follow all conventions in AGENTS.md.\n"
    "2. Implement only the assigned task — do not work ahead to other tasks.\n"
    "3. After completing, append a one-line summary of what you did to memory/status.md.\n"
    "4. End your response with a line starting with 'SUMMARY: ' followed by one sentence on what changed and why."
)

SENSOR_CORRECTIVE_INSTRUCTIONS = (
    "Your last change to this task produced sensor issues. "
    "Fix these issues and nothing else."
)

CODE_HEALTH_CORRECTIVE_INSTRUCTIONS = (
    "Your last change to this task has code-health issues. "
    "Fix these issues and nothing else."
)

REVIEW_CORRECTIVE_INSTRUCTIONS = (
    "A reviewer requested changes to your last change for this task. "
    "Fix these requested changes and nothing else."
)

REVIEWER_STATIC_INSTRUCTIONS = (
    "Review this task's diff against the task description and against "
    "this repo's AGENTS.md conventions."
)


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


def _parse_task_concepts(task_text: str, project_root: Path) -> list[str]:
    """
    Extract specified concept file paths from a task's 'Concepts:' line in plan.md.

    If an explicit 'Concepts:' line is present in task_text, returns absolute paths
    for existing concept files specified on that line.

    If the 'Concepts:' line is omitted, falls back to automated tag-matching:
    scans concept files under memory/concepts/ (excluding index.md) and returns
    concept files whose YAML frontmatter tags match words in task_text.
    """
    concepts_dir = project_root / "memory" / "concepts"
    if not concepts_dir.exists():
        return []

    # 1. Explicit Concepts: line in plan.md
    match = re.search(r'^\s*Concepts:\s*(.+)$', task_text, re.MULTILINE | re.IGNORECASE)
    if match:
        results = []
        for raw in match.group(1).split(","):
            name = raw.strip()
            if not name:
                continue
            if not name.endswith(".md"):
                name = f"{name}.md"
            candidate = concepts_dir / name
            if candidate.exists():
                results.append(str(candidate))
        return results

    # 2. Fallback: Automated tag-matching when Concepts: line is omitted
    results = []
    for concept_file in sorted(concepts_dir.glob("*.md")):
        if concept_file.name == "index.md":
            continue
        try:
            content = concept_file.read_text()
        except OSError:
            continue

        tags_match = re.search(r'^\s*tags:\s*\[(.*?)\]', content, re.MULTILINE | re.IGNORECASE)
        if tags_match:
            tags = [t.strip().lower() for t in tags_match.group(1).split(",") if t.strip()]
        else:
            tags = [concept_file.stem.lower()]

        for tag in tags:
            pattern = r'\b' + re.escape(tag) + r'\b'
            if re.search(pattern, task_text, re.IGNORECASE):
                results.append(str(concept_file))
                break

    return results


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
    context_files: list[str] | None = None,
) -> tuple[list[tuple[str, str]], int]:
    """
    Run sensors, retrying up to SENSOR_RETRY_LIMIT times with a corrective
    worker call in between. Returns (failures, attempt) — failures is the
    (possibly still non-empty) failures list, now at most one entry since
    _run_sensors stops at the first failing sensor per pass (does not decide
    fail-closed itself, that's the caller's job); attempt is the number of
    retries used, 0 when sensors passed on the first try.
    """
    print(f"[sensor:start] Task {i}/{total}: Running sensors (lint.sh, test.sh, lsp.sh)...")
    sys.stdout.flush()
    failures = _run_sensors(worktree)
    attempt = 0
    ctx = context_files if context_files is not None else [agents_abs]
    while failures and attempt < SENSOR_RETRY_LIMIT:
        attempt += 1
        print(
            f"[sensor] Task {i}/{total}: "
            f"{', '.join(name for name, _ in failures)} failed "
            f"(attempt {attempt}/{SENSOR_RETRY_LIMIT})."
        )
        sys.stdout.flush()
        formatted_failures = "\n\n".join(
            f"### {name}\n{output}" for name, output in failures
        )
        corrective_prompt = (
            f"{SENSOR_CORRECTIVE_INSTRUCTIONS}\n\n"
            f"{formatted_failures}"
        )
        corrective_result = driver.run(
            corrective_prompt,
            context_files=ctx,
            cwd=worktree,
        )
        if corrective_result.exit_code != 0:
            print(
                f"[error] Corrective worker call failed on task {i}/{total}:\n"
                f"{corrective_result.text}"
            )
            sys.stdout.flush()
            break

        failures = _run_sensors(worktree)

    if not failures:
        attempt_str = "1st attempt" if attempt == 0 else f"attempt {attempt + 1}"
        print(f"[sensor:done]  Task {i}/{total}: OK (passed on {attempt_str})")
        sys.stdout.flush()

    return failures, attempt


def _run_code_health_with_retry(
    worktree: Path,
    i: int,
    total: int,
    plan_abs: str,
    agents_abs: str,
    status_abs: str,
    driver: AgentDriver,
    context_files: list[str] | None = None,
) -> tuple[list[str], int]:
    """
    Run the lizard-based code-health check, retrying up to
    CODE_HEALTH_RETRY_LIMIT times with a corrective worker call in between.

    Unlike _run_sensors_with_retry, does not decide fail-closed and does not
    re-check sensors after each corrective call — code-health corrections are
    narrower, and the review step that runs immediately after this one already
    re-checks sensors on its own corrective calls. Returns (findings, attempt) —
    findings is the (possibly still non-empty) list of remaining findings;
    attempt is the number of retries used, 0 when clean on the first try.
    """
    print(f"[code-health:start] Task {i}/{total}: Running code health checks (lizard)...")
    sys.stdout.flush()
    findings = check_code_health(worktree)
    attempt = 0
    ctx = context_files if context_files is not None else [agents_abs]
    while findings and attempt < CODE_HEALTH_RETRY_LIMIT:
        attempt += 1
        print(
            f"[code-health] Task {i}/{total}: {len(findings)} finding(s) "
            f"(attempt {attempt}/{CODE_HEALTH_RETRY_LIMIT})."
        )
        sys.stdout.flush()
        formatted_findings = "\n".join(
            f"{n}. {finding}" for n, finding in enumerate(findings, start=1)
        )
        corrective_prompt = (
            f"{CODE_HEALTH_CORRECTIVE_INSTRUCTIONS}\n\n"
            f"{formatted_findings}"
        )
        corrective_result = driver.run(
            corrective_prompt,
            context_files=ctx,
            cwd=worktree,
        )
        if corrective_result.exit_code != 0:
            print(
                f"[error] Corrective worker call failed on task {i}/{total}:\n"
                f"{corrective_result.text}"
            )
            sys.stdout.flush()
            break

        findings = check_code_health(worktree)

    if findings:
        print(f"[code-health:done] Task {i}/{total}: {len(findings)} finding(s) remaining")
    else:
        attempt_str = "1st attempt" if attempt == 0 else f"attempt {attempt + 1}"
        print(f"[code-health:done] Task {i}/{total}: OK (0 findings on {attempt_str})")
    sys.stdout.flush()

    return findings, attempt


def _run_single_review(
    driver: AgentDriver,
    worktree: Path,
    task_text: str,
    context_files: list[str] | None = None,
) -> tuple[bool, str]:
    """Run one reviewer sub-agent pass over the task's current diff."""
    diff = _task_diff(worktree)
    review_prompt = (
        f"{REVIEWER_STATIC_INSTRUCTIONS}\n\n"
        f"Task:\n{task_text}\n\n"
        f"Diff:\n```diff\n{diff}\n```"
    )
    review_result = driver.run_subagent(
        REVIEWER_AGENT, review_prompt, context_files=context_files, cwd=worktree
    )
    return _review_verdict(review_result.text)


def _apply_review_corrective(
    driver: AgentDriver, critique: str, ctx: list[str], worktree: Path, i: int, total: int
) -> bool:
    """Run a corrective worker call for a review critique. Returns True on success."""
    corrective_prompt = f"{REVIEW_CORRECTIVE_INSTRUCTIONS}\n\n{critique}"
    corrective_result = driver.run(corrective_prompt, context_files=ctx, cwd=worktree)
    if corrective_result.exit_code != 0:
        print(
            f"[error] Corrective worker call failed on task {i}/{total}:\n"
            f"{corrective_result.text}"
        )
        sys.stdout.flush()
        return False
    return True


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
    context_files: list[str] | None = None,
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
    ctx = context_files if context_files is not None else [agents_abs]
    while True:
        print(f"[review:start] Task {i}/{total}: Running adversarial review ({REVIEWER_AGENT})...")
        sys.stdout.flush()
        approved, critique = _run_single_review(driver, worktree, task_text, context_files=ctx)

        if approved:
            review_critiques.pop(i, None)
            print(f"[review:done]  Task {i}/{total}: APPROVED — \"{critique}\"")
            sys.stdout.flush()
            return approved, critique, review_attempt, sensor_retry_count, []

        if review_attempt >= REVIEW_RETRY_LIMIT:
            print(
                f"[review] Task {i}/{total}: review budget exhausted "
                f"({REVIEW_RETRY_LIMIT}/{REVIEW_RETRY_LIMIT}) — committing with "
                f"outstanding critique."
            )
            sys.stdout.flush()
            review_critiques[i] = critique
            return approved, critique, review_attempt, sensor_retry_count, []

        review_attempt += 1
        print(
            f"[review] Task {i}/{total}: changes requested "
            f"(attempt {review_attempt}/{REVIEW_RETRY_LIMIT})."
        )
        sys.stdout.flush()
        print(f"[worker:corrective] Task {i}/{total}: Applying reviewer correction...")
        sys.stdout.flush()
        if not _apply_review_corrective(driver, critique, ctx, worktree, i, total):
            review_critiques[i] = critique
            return approved, critique, review_attempt, sensor_retry_count, []

        failures, attempt = _run_sensors_with_retry(
            worktree, i, total, plan_abs, agents_abs, status_abs, driver, context_files=ctx
        )
        sensor_retry_count += attempt
        if failures:
            print(
                f"[error] Sensors still failing on task {i}/{total}: "
                f"{', '.join(name for name, _ in failures)}."
            )
            return approved, critique, review_attempt, sensor_retry_count, failures


MAX_DIFF_LINES = 500


def _task_diff(worktree: Path) -> str:
    """
    Capture the worktree's current uncommitted changes as a diff string.

    Stages everything first (harmless pre-staging — _commit_task does its
    own git add -A right before committing) so new/modified/deleted files
    all show up, since sub-agents like the reviewer have no Bash and can't
    run git diff themselves.

    Caps raw output at MAX_DIFF_LINES lines to avoid flooding prompt context.
    """
    subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True, check=False)
    result = subprocess.run(
        ["git", "diff", "--cached", "HEAD"],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    diff = result.stdout
    lines = diff.splitlines()
    if len(lines) > MAX_DIFF_LINES:
        truncated_count = len(lines) - MAX_DIFF_LINES
        diff = (
            "\n".join(lines[:MAX_DIFF_LINES])
            + f"\n\n... [diff truncated: {truncated_count} additional lines omitted]"
        )
    return diff


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
        code_health_findings = entry["code_health_findings"]
        if code_health_findings:
            lines.append(f"Code health: {len(code_health_findings)} finding(s) remaining")
            for finding in code_health_findings:
                lines.append(f"  - {finding}")
        else:
            lines.append("Code health: clean")
        sensor_retries = entry["sensor_retries"]
        review_retries = entry["review_retries"]
        code_health_retries = entry["code_health_retries"]
        if sensor_retries or review_retries or code_health_retries:
            parts = []
            if sensor_retries:
                noun = "retry" if sensor_retries == 1 else "retries"
                parts.append(f"{sensor_retries} sensor {noun}")
            if code_health_retries:
                noun = "retry" if code_health_retries == 1 else "retries"
                parts.append(f"{code_health_retries} code-health {noun}")
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


def _show_diff_in_editor(
    branch: str,
    project_root: Path,
    critiques: dict[int, str] | None = None,
    code_health_issues: dict[int, list[str]] | None = None,
) -> None:
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
    if code_health_issues:
        lines = ["# Outstanding code-health findings", ""]
        for task_num, findings in code_health_issues.items():
            lines.append(f"## Task {task_num}")
            for finding in findings:
                lines.append(f"- {finding}")
            lines.append("")
        content = "\n".join(lines) + "\n" + content
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


def _perform_squash_merge(branch: str, project_root: Path, task: str, commits: list[str]) -> str:
    """Squash branch's commits into one commit on the current branch, deleting branch on success."""
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


def _offer_merge(
    branch: str,
    project_root: Path,
    task: str = "",
    critiques: dict[int, str] | None = None,
    narrative_path: Path | None = None,
    code_health_issues: dict[int, list[str]] | None = None,
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

    if code_health_issues:
        print("\n[code-health] Outstanding findings from unresolved code-health checks:")
        for task_num, findings in code_health_issues.items():
            print(f"  Task {task_num}:")
            for finding in findings:
                print(f"    - {finding}")

    _show_diff_in_editor(branch, project_root, critiques, code_health_issues)
    if narrative_path is not None and "ZELLIJ" in os.environ:
        _zellij_edit(str(narrative_path))

    answer = input("\n[merge] Squash-merge into current branch? (y/n) ").strip().lower()
    if answer != "y":
        print(f"[merge] Branch '{branch}' preserved. To squash-merge manually:")
        print(f"  git merge --squash {branch} && git commit && git branch -D {branch}")
        return "declined"

    return _perform_squash_merge(branch, project_root, task, commits)


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


# ── run_loop helpers ─────────────────────────────────────────────────────────

def _check_plan_result(plan_result, plan_path: Path) -> int | None:
    """Return an exit code if the planner subprocess itself failed, else None."""
    if plan_result.exit_code != 0:
        print(f"[error] Planner exited with code {plan_result.exit_code}:\n{plan_result.text}")
        return 1
    if not plan_path.exists():
        print(f"[error] Planner did not write {PLAN_FILE}.\nPlanner output:\n{plan_result.text}")
        return 1
    return None


def _generate_plan(driver: AgentDriver, task: str) -> int:
    """
    Run the planner, with corrective retries, until plan.md is valid.

    Writes plan.md as a side effect (via the planner sub-agent). Returns 0 on
    success, 1 on error (planner crash, missing plan.md, or a plan still
    invalid after PLANNER_RETRY_LIMIT retries).
    """
    print(f"\n[planner:start] Task: {task!r}")
    print("[planner:start] Exploring codebase and writing plan...")
    sys.stdout.flush()

    plan_result = driver.run_subagent(
        PLANNER_AGENT,
        f"Task: {task}\n\nExplore the codebase and write a plan to plan.md following your instructions.",
    )
    plan_path = Path(PLAN_FILE)

    exit_code = _check_plan_result(plan_result, plan_path)
    if exit_code is not None:
        return exit_code

    attempt = 0
    invalid_reason = _plan_invalid_reason(plan_result.text, PLAN_FILE)
    while invalid_reason is not None and attempt < PLANNER_RETRY_LIMIT:
        attempt += 1
        print(
            f"[planner:warn]  Plan invalid (attempt {attempt}/{PLANNER_RETRY_LIMIT}): {invalid_reason}"
        )
        sys.stdout.flush()

        corrective_prompt = (
            f"Task: {task}\n\n"
            f"Your previous plan.md was invalid: {invalid_reason}\n\n"
            f"Rewrite plan.md. It must end with the exact line "
            f'"{PLAN_READY_SIGNAL} — awaiting approval." and must contain a '
            "'## Tasks' section with a numbered list of tasks (e.g. '1. **title** — …')."
        )
        plan_result = driver.run_subagent(PLANNER_AGENT, corrective_prompt)

        exit_code = _check_plan_result(plan_result, plan_path)
        if exit_code is not None:
            return exit_code

        invalid_reason = _plan_invalid_reason(plan_result.text, PLAN_FILE)

    if invalid_reason is not None:
        print(
            f"[error] Planner still produced an invalid plan after "
            f"{PLANNER_RETRY_LIMIT} retries: {invalid_reason}"
        )
        sys.stdout.flush()
        return 1

    planner_cost_str = f" (${plan_result.cost_usd:.4f})" if plan_result.cost_usd is not None else ""
    print(f"[planner:done]  Plan written to {PLAN_FILE}{planner_cost_str}")
    sys.stdout.flush()
    return 0


def _handle_sensor_failure(failures: list[tuple[str, str]], i: int, total: int, handle) -> int:
    """Print the sensor-failure stop message, preserve the branch, and return exit code 1."""
    print(
        f"[error] Sensors still failing on task {i}/{total}: "
        f"{', '.join(name for name, _ in failures)}."
    )
    print(f"[loop] Stopped at task {i}. Completed: {i - 1}/{total}.")
    sys.stdout.flush()
    handle.keep()
    if handle.branch:
        print(
            f"[loop] Branch '{handle.branch}' preserved — {i - 1} completed "
            f"task(s) are not lost. Inspect with `git log {handle.branch} --oneline`, "
            f"or merge manually with `git merge --squash {handle.branch} && git commit`."
        )
        sys.stdout.flush()
    return 1


def _run_task_checks(
    driver: AgentDriver,
    handle,
    i: int,
    total: int,
    task_text: str,
    plan_abs: str,
    agents_abs: str,
    status_abs: str,
    task_context: list[str],
    review_critiques: dict[int, str],
) -> tuple[dict | None, list[tuple[str, str]]]:
    """
    Run sensors, code-health, and adversarial review for a task the worker has
    already implemented.

    Returns (narrative_partial, failures). On success narrative_partial holds
    the review/code-health/retry fields (the caller still needs to add
    num/title/summary) and failures is empty. On an unresolved sensor failure
    narrative_partial is None and failures is non-empty — the caller must stop
    the loop.
    """
    sensor_retry_count = 0

    failures, attempt = _run_sensors_with_retry(
        handle.path, i, total, plan_abs, agents_abs, status_abs, driver, context_files=task_context
    )
    sensor_retry_count += attempt
    if failures:
        return None, failures

    # ── Code health (lizard: complexity, size, duplication) ──────────────────
    findings, code_health_retry_count = _run_code_health_with_retry(
        handle.path, i, total, plan_abs, agents_abs, status_abs, driver, context_files=task_context
    )

    # ── Adversarial review ────────────────────────────────────────────────────
    approved, critique, review_attempt, review_sensor_retry_count, failures = (
        _run_review_with_retry(
            handle.path,
            task_text,
            i,
            total,
            plan_abs,
            agents_abs,
            status_abs,
            driver,
            review_critiques,
            context_files=task_context,
        )
    )
    sensor_retry_count += review_sensor_retry_count
    if failures:
        return None, failures

    return {
        "review_approved": approved,
        "review_reasoning": critique,
        "code_health_findings": findings,
        "sensor_retries": sensor_retry_count,
        "review_retries": review_attempt,
        "code_health_retries": code_health_retry_count,
    }, []


def _warn_if_leaked(
    i: int, total: int, project_root: Path, status_abs: str, main_dirty_before: list[str]
) -> None:
    """Warn if the worker wrote to the main checkout instead of its sandboxed worktree."""
    main_dirty_after = _main_checkout_dirty_paths(project_root, status_abs)
    leaked = sorted(set(main_dirty_after) - set(main_dirty_before))
    if leaked:
        print(
            f"[warning] Task {i}/{total}: the worker wrote outside its "
            f"sandboxed worktree, into the main checkout: {', '.join(leaked)}. "
            f"This should not happen (see AGENTS.md sandboxing gotchas) — "
            f"inspect and resolve with `git status`/`git diff` before merging."
        )
        sys.stdout.flush()


def _print_task_metrics(i: int, total: int, run_metrics: Metrics, calls_before: int, cost_before: float) -> None:
    task_calls = run_metrics.calls - calls_before
    task_cost = run_metrics.cost_usd - cost_before
    print(
        f"[metrics] Task {i}/{total}: {task_calls} driver call(s), "
        f"${task_cost:.4f}, session {run_metrics.last_session_id}"
    )
    sys.stdout.flush()


def _run_one_task(
    driver: AgentDriver,
    handle,
    i: int,
    total: int,
    task_text: str,
    plan_abs: str,
    agents_abs: str,
    status_abs: str,
    project_root: Path,
    review_critiques: dict[int, str],
    run_metrics: Metrics,
) -> int | tuple[dict, list[str]]:
    """
    Implement and validate one task: worker call, sensors, code-health,
    review, commit, and per-task metrics reporting.

    Returns an exit code (1) if the loop must stop — a worker failure (branch
    discarded, handle.keep() not called) or unresolved sensor failures
    (branch preserved via _handle_sensor_failure) — or (narrative,
    code_health_findings) on success.
    """
    task_concepts = _parse_task_concepts(task_text, project_root)
    task_context = [agents_abs] + task_concepts
    concepts_str = f" (Concepts: {', '.join(Path(c).name for c in task_concepts)})" if task_concepts else ""

    print(f"\n[worker:start]  Task {i}/{total}: {_task_title(task_text)}{concepts_str}")
    sys.stdout.flush()

    calls_before, cost_before = run_metrics.calls, run_metrics.cost_usd
    status_hash_before = _file_hash(status_abs)
    main_dirty_before = _main_checkout_dirty_paths(project_root, status_abs)

    worker_prompt = (
        f"{WORKER_STATIC_INSTRUCTIONS}\n\n"
        f"Task to implement from {plan_abs} (today: {datetime.now(tz=UTC).date().isoformat()}):\n"
        f"{task_text}"
    )
    worker_result = driver.run(worker_prompt, context_files=task_context, cwd=handle.path)

    if worker_result.exit_code != 0:
        print(f"[error] Worker failed on task {i}/{total}:\n{worker_result.text}")
        print(f"[loop] Stopped at task {i}. Completed: {i - 1}/{total}.")
        sys.stdout.flush()
        return 1  # handle._keep is False → sandbox discards the branch

    worker_summary = _worker_summary(worker_result.text)
    cost_str = f" (${worker_result.cost_usd:.4f})" if worker_result.cost_usd is not None else ""
    print(f"[worker:done]   Task {i}/{total}: OK{cost_str} — \"{worker_summary}\"")
    sys.stdout.flush()

    if _file_hash(status_abs) == status_hash_before:
        print(f"[warning] Worker did not update {STATUS_MD} after task {i}.")
        sys.stdout.flush()

    narrative_partial, failures = _run_task_checks(
        driver, handle, i, total, task_text, plan_abs, agents_abs, status_abs, task_context, review_critiques
    )
    if narrative_partial is None:
        return _handle_sensor_failure(failures, i, total, handle)

    narrative = {"num": i, "title": _task_title(task_text), "summary": worker_summary, **narrative_partial}

    _warn_if_leaked(i, total, project_root, status_abs, main_dirty_before)
    _commit_task(i, _task_title(task_text), handle.path)
    _print_task_metrics(i, total, run_metrics, calls_before, cost_before)

    return narrative, narrative_partial["code_health_findings"]


def _finalize_run(
    task: str,
    task_narratives: list[dict],
    project_root: Path,
    run_timestamp: str,
    handle,
    review_critiques: dict[int, str],
    code_health_issues: dict[int, list[str]],
    run_metrics: Metrics,
) -> None:
    """Write the run narrative, offer a merge, and print/append final metrics."""
    narrative_content = _build_narrative(task, task_narratives)
    narrative_path = _write_narrative(project_root, run_timestamp, narrative_content)

    if handle.branch:  # "" when NoopSandbox is active (tests / no-git projects)
        outcome = _offer_merge(
            handle.branch,
            project_root,
            task,
            review_critiques,
            narrative_path=narrative_path,
            code_health_issues=code_health_issues,
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


def _implement_tasks(
    driver: AgentDriver,
    sandbox,
    tasks: list[str],
    plan_abs: str,
    agents_abs: str,
    status_abs: str,
    project_root: Path,
    run_metrics: Metrics,
):
    """
    Implement all tasks inside one sandbox workspace, one task at a time.

    Returns (exit_code, handle, task_narratives, review_critiques,
    code_health_issues). exit_code is None on success, with handle.keep()
    already called; otherwise the caller should return exit_code immediately
    (the failing task already handled its own branch-preservation decision).
    """
    review_critiques: dict[int, str] = {}
    code_health_issues: dict[int, list[str]] = {}
    task_narratives: list[dict] = []

    with sandbox.workspace(project_root) as handle:
        for i, task_text in enumerate(tasks, 1):
            result = _run_one_task(
                driver, handle, i, len(tasks), task_text, plan_abs, agents_abs, status_abs,
                project_root, review_critiques, run_metrics,
            )
            if isinstance(result, int):
                return result, handle, task_narratives, review_critiques, code_health_issues

            narrative, findings = result
            if findings:
                code_health_issues[i] = findings
            task_narratives.append(narrative)

        handle.keep()  # all tasks complete → preserve the branch for merge

    return None, handle, task_narratives, review_critiques, code_health_issues


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

    exit_code = _generate_plan(driver, task)
    if exit_code != 0:
        return exit_code

    approved = gate.request(PLAN_FILE)
    if not approved:
        print("[loop] Plan rejected — exiting without changes.")
        _append_status(
            f"\n## {datetime.now(tz=UTC).date().isoformat()} — plan rejected\n"
            f"Task: {task}\n"
            f"Plan written but not approved by human.\n"
        )
        return 2

    tasks = _parse_tasks(PLAN_FILE)
    if not tasks:
        print(
            f"[error] No numbered tasks found in {PLAN_FILE}. "
            "The plan must have a '## Tasks' section with items like '1. **title** — …'"
        )
        return 1

    print(f"\n[loop] {len(tasks)} task(s) to implement.")

    exit_code, handle, task_narratives, review_critiques, code_health_issues = _implement_tasks(
        driver, sandbox, tasks, plan_abs, agents_abs, status_abs, project_root, run_metrics,
    )
    if exit_code is not None:
        return exit_code

    _finalize_run(
        task, task_narratives, project_root, run_timestamp, handle,
        review_critiques, code_health_issues, run_metrics,
    )

    print(f"\n[loop] All {len(tasks)} tasks complete.")
    return 0


# Entry point is cli.py:main — run via `agent loop [task]`
