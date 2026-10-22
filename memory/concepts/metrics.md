---
type: component
tags: [metrics, cost, session, telemetry]
summary: Driver call accounting, cost tracking, and session ID recording across all agent operations.
links: [docs/arch/adr/0012-metrics-metered-driver-wrapper.md]
updated: 2026-10-22
verified: [{ by: "human", at: 2026-10-22 }]
---

# Driver Metrics & Cost Tracking

## Context & Rationale
Scaling autonomous loops requires real-time visibility into API call frequency and financial costs. A single accumulation point ensures cost and session tracking apply universally across all sub-agents without modifying individual driver implementations.

## Key Rules & Mechanics
- **`Metrics` Dataclass (`runner/metrics.py`):** Holds accumulated `calls`, `cost_usd`, `last_session_id`, run-total token counters (`input_tokens` = uncached input, `cache_read_tokens`, `cache_write_tokens`), and `by_label` (label → `LabelMetrics` with calls, cost, and the same three counters). `record(result, label=None)` skips `None` cost/token values; without a label only the totals update.
- **`_MeteredDriver` Wrapper (`runner/metrics.py`):** Wraps any concrete `AgentDriver` (e.g. `ClaudeDriver`). Automatically intercepts `run()` and `run_subagent()` calls to record cost and session telemetry into the shared `Metrics` instance.
- **Token capture:** `ClaudeDriver` reads the CLI JSON's top-level snake_case `usage` object (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`; cumulative over all turns of the `--print` session — not the camelCase per-model `modelUsage`). Missing, non-dict, non-int (incl. `bool`) values become `None` on `AgentResult`; parsing never raises.
- **Labels:** `_MeteredDriver.run_subagent()` records under the agent name; `run()` records under `"corrective"` (`CORRECTIVE_LABEL`), since plain `run()` is only used for corrective retries.
- **Cache-read share:** `cache_read / (cache_read + cache_write + input_tokens)`; `None` (rendered `n/a`) when the denominator is 0, so "no data" is never shown as 0%.
- **Per-Task Snapshotting:** In `runner/loop.py`, the loop snapshots `calls` and `cost_usd` before and after each task, logging per-task spend (`[metrics] Task i/N: X call(s), $Y.YY`).
- **Run-Total Reporting:** At loop completion, the total driver call count and dollar spend are printed to console and appended to `memory/status.md`, followed by a `[metrics] Run cache:` line and one sorted line per label; the status line ends with `, cache read N / write N / input N (share X%)`. Per-task lines carry no cache data.

## Gotchas & Failure Modes
- **Sub-Agent Call Attribution:** Planner and standalone review calls accumulate in total run metrics but are not attributed to individual task iterations.
