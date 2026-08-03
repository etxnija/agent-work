# ADR-0010: Sensor feedback with corrective retry

## Status

Accepted

## Context

Per-task implementation (ADR-0008) made diffs small enough that lint, test, and type
errors are locally actionable. But the loop had no way to catch these before a human
saw them — a task could commit sensor-failing code with no feedback loop.

## Decision

`_run_sensors()` runs every `sensors/*.sh` script uniformly and returns the failures.
`run_loop()` calls it after each task; on failure, it sends the sensor output back to
the worker as a corrective prompt and re-checks, up to `SENSOR_RETRY_LIMIT` times. If
sensors still fail after the retry budget, the task fails closed: no commit, loop
stops, same outcome as a hard worker failure. There is no per-sensor-type logic — new
sensors plug in for free by dropping a script in `sensors/`.

`sensors/lsp.sh` uses Python's one-shot batch-diagnostic CLI (`pyright --outputjson`)
rather than a persistent LSP client or JSON-RPC session. This shares the same
diagnostic engine as `pyright-langserver`, so results match what a human sees in their
editor, and keeps the sensor a plain standalone shell script with no new protocol
dependency in `runner/`. The same batch-CLI-over-persistent-client reasoning applies to
`tsc`/`gopls` when those languages are added later, even though only Python is wired up
now.

## Consequences

- Each retry is a full extra worker (LLM) call — up to `SENSOR_RETRY_LIMIT` per task.
- A task that never converges fails closed rather than committing sensor-failing code.
- Errors-only severity (pyright's default exit-code behavior) is a design assumption,
  not yet validated against a real run.
