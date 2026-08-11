---
type: pattern
tags: [invest, loop, task, commits, plan]
summary: INVEST task decomposition, plan verification, per-task commit hooks, and merge workflow.
links: [docs/arch/adr/0008-per-task-implementation.md]
updated: 2026-08-11
---

# Task Decomposition & Loop Execution

## Context & Rationale
Large, un-bounded plans lead to massive diffs, complex tool turns, and un-reviewable PRs. Task decomposition forces plans into small, independent, testable units (INVEST principle).

## Key Rules & Mechanics
- **Planner Sub-Agent (`agents/planner.md`):** Operates on `model: claude-opus-4-6` with read-only tools (`Read, Glob, Grep, Write`). Explores the codebase and outputs `plan.md`.
- **`## Tasks` Specification:** `plan.md` must contain a numbered list under `## Tasks`. Each task defines `1. **Title** — description`, `Files: ...`, `What: ...`.
- **Plan Validation Before Approval:** The loop verifies `PLAN READY` and non-empty `_parse_tasks()` before requesting human approval (`gate.request()`), retrying up to `PLANNER_RETRY_LIMIT = 2` times if invalid.
- **Per-Task Execution Loop:** The harness implements tasks sequentially one at a time. Each task is executed in the worktree, validated against sensors/code-health/review, and committed individually via `_commit_task()`.
- **Branch Preservation on Failure:** If a task fails or exhausts retries, `handle.keep()` preserves the branch and completed commits instead of running `git branch -D`.
- **Squash-Merge Prompt (`_offer_merge`):** After all tasks succeed, the loop offers an interactive squash-merge into main, displaying commits and opening the branch diff in a floating Zellij pane (`_show_diff_in_editor`).

## Gotchas & Failure Modes
- **Task Oversizing:** Tasks touching more than 2–3 files increase tool-turn complexity inside Claude Code, driving up token consumption rapidly.
- **Plan Sequencer Dependencies:** Tasks must be ordered so caller and callee changes land together to avoid intermediate sensor failures between tasks.
