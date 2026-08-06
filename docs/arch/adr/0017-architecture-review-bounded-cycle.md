# ADR-0017: Architecture review via bounded CLAIM→DOUBT→RECONCILE cycle

## Status

Accepted

## Context

The refactor agent (ADR — none yet, see `agents/refactor.md`) flags pattern drift:
dead code, single-use-but-reusable helpers, inconsistent naming. It cannot judge
whether a module's shape still makes sense — coupling, cohesion, and responsibility
boundaries are a harder, open-ended call that a single read-only pass tends to get
either too confidently wrong or too vague to act on.

## Decision

An architect sub-agent (`agents/architect.md`, `Read`/`Glob`/`Grep` only, same model
as the planner) runs a bounded three-role cycle instead of a single-shot critique:
CLAIM+EXTRACT produces one falsifiable claim with evidence, DOUBT actively tries to
falsify it, RECONCILE decides whether it survives and signals via marker line —
`ARCHITECTURE: CONVERGED` or `ARCHITECTURE: REVISED` — mirroring the reviewer's
`REVIEW: APPROVED`/`CHANGES REQUESTED` convention. On REVISED, the cycle repeats with
the new claim, capped at `ARCH_MAX_ROUNDS = 3`. Before forming or revising any claim,
the agent must read `AGENTS.md`, `docs/roadmap.md`, and relevant `docs/arch/adr/*.md`
files, so a claim can't ignore a documented reason the current shape is intentional.

Calls are metered via `_MeteredDriver` (ADR-0012). The final recommendation is written
to `architecture-recommendation.md` and paused on via the existing gate
(`get_gate().request(...)`) for human review, same checkpoint pattern as `plan.md`.
The new `runner/architecture.py` module is self-contained: it imports only from
`runner/metrics.py`, `runner/drivers`, and `runner/gates` — not from `runner/loop.py`,
and `runner/loop.py` does not import from it either.

## Consequences

- Up to 9 opus calls per review (1 CLAIM + 3 × (DOUBT + RECONCILE)) — visible via the
  printed `[metrics]` line and the recommendation file's own metrics footer.
- Keeping the module self-contained avoids coupling a genuinely different control-flow
  shape (bounded debate cycle) onto the task-loop shape in `runner/loop.py`.
- `_architecture_verdict` duplicates `_review_verdict`'s parsing pattern rather than
  sharing code — an accepted maintenance surface, not an oversight.
- Manual invocation only (`agent architect <path>`), same as `agent refactor` — no
  auto-triggering from the planner or refactor agents.
