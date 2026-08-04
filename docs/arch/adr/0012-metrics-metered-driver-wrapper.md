# ADR-0012: Metered-driver wrapper as the metrics accumulation point

## Status

Accepted

## Context

`run_loop()` has three driver call sites today: the planner (`run_subagent`), the worker (`run`), and the sensor-corrective-retry (`run`). Phase 2.3 is about to add a fourth — a review-corrective-retry. Cost and call-count visibility needs to hold at every call site, including ones added later, not just the ones someone remembers to instrument by hand.

## Decision

Wrap whatever `get_driver()` returns in a `_MeteredDriver` — implements `AgentDriver`, delegates `run`/`run_subagent` to the wrapped instance, and records each returned `AgentResult` into a shared `Metrics` instance (`calls`, `cost_usd`). `run_loop()` rebinds `driver` to this wrapper once, immediately after `get_driver()`, so every existing and future call site is metered automatically instead of adding an explicit record-call at each one. Per-task numbers come from snapshotting the single `Metrics` instance before and after each task rather than keeping a second per-task `Metrics` object, since the loop is single-threaded and strictly sequential.

`_MeteredDriver` is deliberately *not* a fourth pluggable backend behind `get_driver()`'s `AGENT_TOOL` env-var pattern — it's an internal always-on wrapper, not a swappable implementation.

## Consequences

- New call sites (e.g. Phase 2.3's reviewer) get metrics for free by construction — no new convention to remember or enforce.
- Adding a second metric (wall-clock time, retry counts) is a new `Metrics` field plus a couple of lines inside `_MeteredDriver`, not a new call-site convention.
- Per-task snapshotting assumes strictly sequential execution; a future concurrent loop would need per-task `Metrics` instances instead of before/after diffing the shared one.
