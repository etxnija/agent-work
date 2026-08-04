# ADR-0013: Adversarial review via marker-line verdict, non-blocking on exhaustion

## Status

Accepted

## Context

Sensors (ADR-0010) catch what a script can check — lint, tests, type errors. Some
defects are judgment calls instead: does this diff actually match the task description
and follow `AGENTS.md`'s conventions? That needs a second model's read, not a script.
ADR-0012 already reserved the metrics accumulation point for this fourth driver call
site.

## Decision

A reviewer sub-agent (`agents/reviewer.md`, `Read`/`Glob`/`Grep` only — no `Write`, no
`Bash`) inspects the task's diff and the worktree, then signals its verdict via a
marker line: `REVIEW: APPROVED` or `REVIEW: CHANGES REQUESTED` followed by a critique.
This is string-matched the same way as the planner's `PLAN READY`, not structured or
JSON output. The review runs after sensors pass and before commit. On disagreement,
the critique becomes a corrective worker prompt — same shape as sensor feedback — and
both sensors and the review re-run, up to `REVIEW_RETRY_LIMIT = 2` times.

Unlike sensor failures, exhausting the review retry budget does not discard the
branch. The task still commits, with the outstanding critique attached, and the
critique is surfaced at the merge-diff review step (`_show_diff_in_editor`) so the
human sees the disagreement before deciding to squash-merge.

## Consequences

- A stuck reviewer costs up to `REVIEW_RETRY_LIMIT` extra worker calls plus
  `REVIEW_RETRY_LIMIT + 1` review calls per task — visible via Phase 2.2's metrics.
- Disagreements are surfaced, not silently discarded: a bit more noise at merge time,
  but no real work is lost to a model's opinion.
- A genuine sensor regression discovered mid-review-cycle still fails closed exactly
  as before — only the review dimension is non-blocking.
