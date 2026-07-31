# ADR-0004: Approval gate abstraction

## Status

Accepted

## Context

The loop requires human approval of the planner's output before any code is written. Two distinct use cases exist: interactive sessions where a human is at the terminal, and unattended overnight runs (the Ralph loop, Phase 4) where blocking on input is not possible. These need different mechanisms but the loop should not branch on the mode.

## Decision

Define `ApprovalGate` as an abstract base class with a single method: `request(plan_path) -> bool`. A factory function `get_gate()` selects the implementation via the `AGENT_MODE` environment variable.

`InteractiveGate` ships in Phase 1: prints the plan and waits for `y/N` at the terminal.

`FileGate` is deferred to Phase 4: polls for a `plan.approved` sentinel file, enabling headless runs. The interface is already defined; adding it requires one new file and one line in `get_gate()`.

## Consequences

- The loop never changes when headless mode is added.
- The approval is an explicit, auditable pause — the human must act before the worker runs.
- `FileGate` also creates an audit trail: the sentinel file can be committed alongside `plan.md`.
- Interactive-only in Phase 1 means overnight automation is blocked until Phase 4. This is intentional — Phase 1 is attended use only.
