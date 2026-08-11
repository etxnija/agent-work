---
type: pattern
tags: [reviewer, review, haiku, clean-slate, retry]
summary: Adversarial review sub-agent cycle, verdict parsing, and non-blocking critique retries.
links: [docs/arch/adr/0013-adversarial-review-marker-and-non-blocking-retry.md]
updated: 2026-08-11
---

# Adversarial Review

## Context & Rationale
Computational sensors (linters, tests, type checks) catch deterministic bugs, but cannot evaluate design alignment or convention adherence. A second, independent sub-agent critiques completed task diffs before they are committed.

## Key Rules & Mechanics
- **Reviewer Sub-Agent (`agents/reviewer.md`):** Configured with `model: claude-haiku-4-5` and `tools: Read, Glob, Grep`. It reads `AGENTS.md`, inspects surrounding code, and reviews the task's changes.
- **Clean-Slate Isolation:** The Reviewer is passed *only* the INVEST task description and the clean git diff (`_task_diff()`). It does not receive the worker's internal tool turns or reasoning context.
- **Verdict Markers (`_review_verdict`):** The Reviewer signals decisions via explicit marker lines:
  - `REVIEW: APPROVED <one-sentence reasoning>`
  - `REVIEW: CHANGES REQUESTED <actionable critique>`
- **Non-Blocking Retry Budget (`_run_review_with_retry`):** If changes are requested, the critique is fed back to the worker for corrective implementation (up to `REVIEW_RETRY_LIMIT = 2`). Unlike sensors, if the review budget is exhausted, the task commits anyway and the critique is surfaced at merge time.

## Gotchas & Failure Modes
- **Marker Line Parsing:** Marker parsing relies on exact string signals. Ambiguous output without a clear marker defaults conservatively to `CHANGES REQUESTED`.
- **Reviewer Critique Visibility:** Critiques from exhausted review budgets are surfaced in terminal output and in floating Zellij diff-preview panes (`_show_diff_in_editor`).
