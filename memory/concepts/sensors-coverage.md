---
type: component
tags: [sensors, coverage, lint, pyright, code-health]
summary: Computational sensors, linting, LSP type-checking, diff-coverage, and code-health checks.
links: [docs/arch/adr/0010-sensor-retry-mechanism.md]
updated: 2026-08-11
---

# Sensors, Coverage & Code-Health

## Context & Rationale
Deterministic computational checks beat vague LLM critiques for convergent code optimization. Sensors provide fast, un-biased feedback before code is accepted or reviewed by humans.

## Key Rules & Mechanics
- **Sensor Execution (`_run_sensors` in `runner/loop.py`):** Automatically globs and runs executable scripts in `sensors/*.sh` in sorted order (`lint.sh`, `test.sh`, `lsp.sh`). Short-circuits on the first failing script.
- **Sensor Retry Loop (`_run_sensors_with_retry`):** Retries failing sensors up to `SENSOR_RETRY_LIMIT = 2` times with a corrective worker call between attempts. Fails closed (stops execution, preserves branch) if sensors remain broken after retries.
- **Mechanical Auto-Fixes (`sensors/lint.sh`):** Linters run auto-fixers (`ruff check --fix .`) first so mechanical syntax/formatting issues resolve for 0 tokens without spending LLM retry calls.
- **Batch LSP Diagnostics (`sensors/lsp.sh`):** Uses batch one-shot CLI type-checking (`pyright --outputjson`) to feed language-server diagnostics into corrective prompts.
- **Coverage Enforcement:** Requires 100% diff-aware test coverage on new/changed lines (`diff-cover`), backed by a whole-repo regression floor (`.coverage-baseline`) with a 1.0% tolerance.
- **Code-Health Sensor (`runner/code_health.py`):** Uses `lizard` to enforce function length and cyclomatic complexity limits. Runs as a soft gate with retries (`CODE_HEALTH_RETRY_LIMIT = 2`) that logs findings rather than failing closed.

## Gotchas & Failure Modes
- **Sensor Observability:** Sensor failure output is captured in memory for the corrective prompt. If a run errors out, failure details must be printed clearly so un-converged sensor failures are visible in scrollback.
- **Coverage Regressions:** Baseline coverage must only be updated (`.coverage-baseline`) after a successful squash-merge into main.
