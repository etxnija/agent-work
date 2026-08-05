# ADR-0016: Two-check coverage enforcement

## Status

Accepted

## Context

A single flat coverage percentage can't distinguish two different failure
modes: new code shipped without tests, and an old regression test quietly
deleted (whose production code isn't part of the current diff, so a
diff-only check would miss it entirely). The former needs a diff-aware
check; the latter needs a whole-repo comparison against a stable baseline.
Both need to reuse `test.sh`'s single coverage-collecting pytest run rather
than adding a second `sensors/*.sh` file, which would sort ahead of
`test.sh` alphabetically and re-run the whole suite a second time — working
against ADR-0015's short-circuit/cost-ordering goals.

## Decision

`sensors/test.sh` runs `pytest --cov` once, producing `coverage.xml` and
`coverage.json`, then runs two checks in sequence: `diff-cover` against
local `main` at `--fail-under=100` (check 1 — no tolerance, new/changed
lines only), then `sensors/_coverage_floor.py` (check 2 — whole-repo
`baseline - current`, 1-point tolerance, not a fixed floor). The tolerance
is deliberately lenient and regression-only, per the reasoning that a hard
whole-repo floor invites gaming rather than catching real regressions. The
baseline is cached in a tracked `.coverage-baseline` file at the project
root, refreshed once per successful merge by a new
`_update_coverage_baseline()` helper in `runner/loop.py`, rather than
re-running the full suite against `main` inside every task.

## Consequences

- Cache staleness between merges is accepted, not fixed — check 2 always
  compares against the last successful merge, not live `main`.
- The very first run after this lands has no baseline yet; check 2 is a
  no-op (prints a notice, exits 0) until the first merge produces one.
- The 1-point tolerance is hardcoded in `_coverage_floor.py`, not
  configurable per project or per run.
