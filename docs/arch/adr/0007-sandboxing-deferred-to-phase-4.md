# ADR-0007: Full sandboxing deferred to Phase 4; planner Bash tool removed immediately

## Status

Accepted

## Context

"Sandboxed" is a Must-have in the PRD and was listed in Phase 1. Full sandboxing means the worker cannot affect anything outside the project directory — typically enforced by running inside a container or VM. This was not implemented in Phase 1.

Two distinct risks exist:

1. **Planner writes files via shell.** The planner's tool list included `Bash`, which is not read-only. Despite the prompt saying "never write files," nothing enforced this at the tool level.

2. **Worker has unrestricted filesystem access.** `ClaudeDriver` passes `--dangerously-skip-permissions` unconditionally. In attended Phase 1 use the human reviews and approves the plan before the worker runs, which provides a manual safety layer. In unattended Phase 4 runs this safety layer is absent.

## Decision

**Immediate (Phase 1 fix):** Remove `Bash` from the planner's tool list. `Read`, `Glob`, and `Grep` are sufficient for codebase exploration and eliminate the shell-based write path entirely.

**Deferred (Phase 4):** Full worker sandboxing — container or VM isolation — is deferred. The manual approval gate is the safety mechanism for attended Phase 1–3 use.

**Trigger for sandboxing to land:** Before any unattended (Ralph loop / `AGENT_MODE=headless`) run. The `FileGate` (ADR-0004) must not be shipped without sandboxing in place. This is a hard dependency.

## Consequences

- The planner is now genuinely read-only at the tool level, not just by instruction.
- The worker remains unsandboxed through Phase 3. This is acceptable while a human approves every plan.
- Adding Phase 4 features (FileGate, Ralph loop) is blocked until sandboxing lands. This must be reflected in the roadmap.
- Container-based sandboxing (Docker/Podman) is the likely Phase 4 implementation. Podman is already installed in the workspace.
