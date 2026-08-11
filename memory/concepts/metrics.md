---
type: component
tags: [metrics, cost, session, telemetry]
summary: Driver call accounting, cost tracking, and session ID recording across all agent operations.
links: [docs/arch/adr/0012-metrics-metered-driver-wrapper.md]
updated: 2026-08-11
---

# Driver Metrics & Cost Tracking

## Context & Rationale
Scaling autonomous loops requires real-time visibility into API call frequency and financial costs. A single accumulation point ensures cost and session tracking apply universally across all sub-agents without modifying individual driver implementations.

## Key Rules & Mechanics
- **`Metrics` Dataclass (`runner/metrics.py`):** Holds accumulated `calls`, `cost_usd`, and `last_session_id`.
- **`_MeteredDriver` Wrapper (`runner/metrics.py`):** Wraps any concrete `AgentDriver` (e.g. `ClaudeDriver`). Automatically intercepts `run()` and `run_subagent()` calls to record cost and session telemetry into the shared `Metrics` instance.
- **Per-Task Snapshotting:** In `runner/loop.py`, the loop snapshots `calls` and `cost_usd` before and after each task, logging per-task spend (`[metrics] Task i/N: X call(s), $Y.YY`).
- **Run-Total Reporting:** At loop completion, the total driver call count and dollar spend are printed to console and appended to `memory/status.md`.

## Gotchas & Failure Modes
- **Sub-Agent Call Attribution:** Planner and standalone review calls accumulate in total run metrics but are not attributed to individual task iterations.
