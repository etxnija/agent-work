# ADR-0014: Run-narrative timestamp source and two-phase write

## Status

Accepted

## Context

The Phase 2.4 run narrative (`_build_narrative`) needs a run-scoped file name and
needs to be visible in the floating Zellij pane at the same review moment as the
existing diff pane (ADR-0013's `_show_diff_in_editor`). Two details don't have an
obvious single answer: where the timestamp comes from, and when the file gets
written. The diff pane is shown *before* the human's merge y/n decision, but "was
the branch merged" is only known *after* `_offer_merge()` returns — the narrative
spans both sides of that decision.

## Decision

`run_loop()` generates its own `%Y%m%d-%H%M%S` timestamp at the top of the run,
rather than deriving one from `GitWorktreeSandbox`'s branch name. `NoopSandbox`
(tests, non-git projects) has no branch at all, so a branch-derived timestamp
wouldn't generalize — an independent timestamp means `logs/run-<ts>.md` exists
uniformly regardless of which sandbox backend is active.

The file is written in two phases, not two summarization passes: content only
(`_write_narrative`) once all tasks are done, shown in the floating pane via
`_zellij_edit` right alongside the diff pane, before the y/n prompt; then, once
`_offer_merge()`'s return value is known, the outcome (`merged`, `declined`,
`squash failed`, etc.) is appended to the same file (`_append_narrative_outcome`).

## Consequences

- The open editor pane doesn't live-refresh — it shows a point-in-time snapshot of
  the narrative without the outcome, same limitation the diff pane already has.
- The log file itself is the durable record: it does end up with the outcome, just
  not visible in the pane that was already opened.
- Reuses ADR-0013's marker-line-in-prose convention rather than inventing a new
  format; unrelated to ADR-0012's metrics accumulation point, but lands in the same
  session's sibling phase.
