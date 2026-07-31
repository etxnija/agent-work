# ADR-0006: Approval gate supports inline feedback before approving

## Status

Accepted

## Context

The initial gate was binary: approve or reject. In practice, a plan is often directionally correct but missing specifics — testing style, approach preferences, scope adjustments. Rejecting to re-run the planner is wasteful; approving without amendments means the worker implements something subtly wrong.

Stable preferences (test style, code conventions) belong in AGENTS.md as feedforward guides — the planner should know about them before writing the plan, not after. But one-off adjustments still need a mechanism.

## Decision

`InteractiveGate` supports three responses: `y` (approve), `n` (reject), `f` (feedback). Feedback prompts for free-text input and appends it to `plan.md` under a `## Human Feedback` section, then redisplays the amended plan for a final approve/reject decision. The worker receives the full plan including the feedback section and is expected to honour it.

## Consequences

- Stable preferences should still go in AGENTS.md — the gate is not a substitute for feedforward guides.
- The feedback loop is: planner writes plan → human amends → worker implements. No re-planning step; the worker interprets the amendments directly.
- If a plan needs substantial rethinking (not just amendment), reject and re-run is still the right path.
- `FileGate` (Phase 4) will need to support the same feedback mechanism — the sentinel file approach may need to accommodate a feedback file alongside `plan.approved`.
