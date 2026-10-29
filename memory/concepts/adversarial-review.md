---
type: pattern
tags: [reviewer, review, haiku, clean-slate, retry]
summary: Adversarial review sub-agent cycle, verdict parsing, and non-blocking critique retries.
links: [docs/arch/adr/0013-adversarial-review-marker-and-non-blocking-retry.md]
updated: 2026-10-29
verified: [{ by: "human", at: 2026-10-12 }, { by: "human", at: 2026-10-29 }, { by: "human", at: 2026-10-29 }]
---

# Adversarial Review

## Context & Rationale
Computational sensors (linters, tests, type checks) catch deterministic bugs, but cannot evaluate design alignment or convention adherence. A second, independent sub-agent critiques completed task diffs before they are committed.

## Key Rules & Mechanics
- **Reviewer Sub-Agent (`agents/reviewer.md`):** Configured with `model: claude-haiku-4-5` and `tools: Read, Glob, Grep`. It reads `AGENTS.md`, inspects surrounding code, and reviews the task's changes.
- **Clean-Slate Isolation:** The Reviewer is passed *only* the INVEST task description, the structured budgeted diff (`_task_diff()`: stat, renames, per-file fair share, NOTE when partial) and, when present, the Worker's status entry. It does not receive the worker's internal tool turns or reasoning context.
- **Budgeted Diff (`runner/task_diff.py`):** `_task_diff` returns `git diff --cached -M --stat` as a header, a `Renamed without changes:` list for pure renames, then per-file patches sharing a `MAX_DIFF_LINES` budget by water-filling (min 30 lines/file; files beyond `budget // 30` are listed under `Files not shown`). Truncated blocks end with `... [file truncated: N additional lines omitted]`, and a `NOTE: partial diff` line sits under the stat when anything was cut.
- **Worker Status Entry:** The text the Worker appended to `status.md` (`status_guard.appended_text`, collected in `_run_one_task` after the append-only guard) is added to the Reviewer prompt after the diff, capped at `MAX_STATUS_ENTRY_CHARS` (2000). Omitted entirely when empty; the same entry is reused on every retry pass.
- **Verdict Markers (`_review_verdict`):** The Reviewer signals decisions via explicit marker lines:
  - `REVIEW: APPROVED <one-sentence reasoning>`
  - `REVIEW: CHANGES REQUESTED <actionable critique>`
- **Non-Blocking Retry Budget (`_run_review_with_retry`):** If changes are requested, the critique is fed back to the worker for corrective implementation (up to `REVIEW_RETRY_LIMIT = 2`). Unlike sensors, if the review budget is exhausted, the task commits anyway and the critique is surfaced at merge time. The corrective call runs on the Worker's pinned model (see `sensors-coverage.md`).

## Gotchas & Failure Modes
- **Marker Line Parsing:** Marker parsing relies on exact string signals. Ambiguous output without a clear marker defaults conservatively to `CHANGES REQUESTED`.
- **Reviewer Critique Visibility:** Critiques from exhausted review budgets are surfaced in terminal output and in floating Zellij diff-preview panes (`_show_diff_in_editor`). They, along with unresolved code-health findings, are also recorded in the squash commit body under `Outstanding review findings (not resolved before merge):` (one whitespace-collapsed line per item, capped at 600 chars), searchable with `git log --grep "Outstanding review findings"`.
