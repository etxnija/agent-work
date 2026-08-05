# ADR-0015: Sensor short-circuit and shared retry budget

## Status

Accepted

## Context

`sensors/*.sh` scripts are already named/sorted cheap-to-expensive (`lint.sh`,
`lsp.sh`, `test.sh`) by naming coincidence, but ADR-0010's `_run_sensors()` runs
every script regardless of earlier failures. An expensive sensor still runs when a
cheap one already failed, wasting compute and bundling unrelated failures into one
confounded, noisier corrective prompt than necessary.

## Decision

`_run_sensors()` stops at the first failing sensor and returns immediately, instead
of collecting failures from the full sorted set. `SENSOR_RETRY_LIMIT` remains a
single budget shared across the whole per-task retry sequence — it is not reset when
a new sensor surfaces after an earlier one is fixed. A per-sensor budget was
rejected: it would multiply worst-case corrective calls by the number of distinct
failing sensors, working against this phase's cost-cutting goal.

`sensors/lint.sh` (and the bootstrap Python preset) also now runs
`ruff check --fix .` instead of `ruff check .`, applying safe auto-fixes before
reporting failure — a second, independent cost lever this phase, needing no design
discussion beyond `ruff --fix`'s own exit-code semantics (non-zero only if
violations remain after fixing).

## Consequences

- A task with two or more genuinely independent sensor failures now clears them
  serially, one corrective call per sensor, instead of one bundled call fixing both
  — less retry slack than before for that (uncommon) case.
- Most real failures share one root cause (e.g. a syntax error fails lint, lsp, and
  test together) and are still fixed by a single corrective call regardless of
  bundling, so this is expected to rarely bite in practice.
- Fail-closed behavior on budget exhaustion is unchanged.
