# ADR-0009: Phase 1.1 sandboxing — git worktree isolation and tool-grant tightening

## Status

Accepted

## Context

Code review of Phase 1 (2026-07-31) found two sandboxing gaps, with a third addressed in Phase 1.1 follow-up work:

1. **Planner over-permissions.** `ClaudeDriver.run_subagent()` used `--dangerously-skip-permissions` unconditionally, even for the planner, which is documented as read-only. The planner's tool list (`Read, Glob, Grep`) was declared in its frontmatter but never enforced — the flag bypassed all permission checks anyway.

2. **Worker operates in the live working tree.** The worker runs `claude` directly in the user's project directory. A bad or confused worker run dirties the working tree, and cleanup requires manual `git restore` or `git clean`.

3. **`--dangerously-skip-permissions` as the only control plane.** Both agents bypassed all permission checks via this flag. There was no explicit allow-list of expected commands or any hook layer to catch destructive patterns at runtime (e.g. `rm -rf`, `git push --force`).

A wide ideation pass on sandboxing options was recorded in the Obsidian PRD (`/Users/nils/Documents/nils/PRD/Sandboxing.md`). Five options were considered, ranging from git worktrees (cheap, workspace isolation only) to Firecracker microVMs (maximum isolation, high setup cost). The PRD recommends starting with Option 1 (git worktrees + tightened tool grants), introducing a `SandboxRuntime` abstraction at the same time so Option 2 (Anthropic's `sandbox-runtime`) can be swapped in later.

## Decision

### 1. Tool-grant tightening for the planner

`ClaudeDriver.run_subagent()` now reads the `tools:` field from the agent's YAML frontmatter. If present, it uses `--allowedTools <tools>` instead of `--dangerously-skip-permissions`. The planner declares `tools: Read, Glob, Grep`, so it now runs with exactly those tools — no shell access, enforced at the kernel/CLI level rather than by prompt convention alone. Agents with no `tools:` declaration continue to use `--dangerously-skip-permissions` (the worker's case).

### 2. Git worktree isolation for the worker

Each `run_loop()` invocation now creates a disposable git branch + worktree before invoking the worker:

- Branch name: `agent/YYYYMMDD-HHMMSS`
- Worktree directory: a temp directory created by `tempfile.mkdtemp()`
- Worker runs with `cwd=worktree_path`
- Context files (plan.md, AGENTS.md, status.md) are passed as **absolute paths** so the worker can read and write them regardless of its cwd

On success (`handle.keep()` called after all tasks complete): the worktree directory is removed and the branch is preserved, ready for a Phase 3 merge. On failure or rejection: both the worktree directory and the branch are discarded.

### 3. SandboxRuntime abstraction

A `SandboxRuntime` ABC (in `runner/sandbox/base.py`) mirrors the `AgentDriver` / `ApprovalGate` pattern:

- `workspace(project_root: Path)` — context manager yielding a `WorkspaceHandle(path, branch)`
- `WorkspaceHandle.keep()` — signals that the branch should be preserved
- `get_sandbox()` factory — controlled by `AGENT_SANDBOX` env var

Concrete implementations:
- `GitWorktreeSandbox` — the Phase 1.1 default (`AGENT_SANDBOX=worktree`)
- `NoopSandbox` — pass-through; worker runs in project_root (`AGENT_SANDBOX=noop`, used in tests)

This structure means Option 2 (Anthropic's `sandbox-runtime`) can be introduced as a new `SandboxRuntime` subclass without touching the loop.

### 4. Permission allow/deny list + PreToolUse hook (defense-in-depth)

Two Claude Code mechanisms are added on top of the tool-grant and worktree layers:

**`.claude/settings.json` permissions block:**
- `allow`: pre-approves exactly what the worker is expected to run non-interactively (e.g. `pytest*`, `git commit*`, `mise run*`). Language-specific commands are added at bootstrap time.
- `deny`: explicitly blocks known-dangerous patterns (`rm -rf*`, `git push*`, `curl*`, `pip install*`, secrets file reads).

**`.claude/hooks/block-destructive.sh` (PreToolUse hook):**
A Python script registered for the `Bash` tool. It receives the full tool input JSON on stdin and pattern-matches `tool_input.command` against destructive patterns the static deny list cannot fully express: `rm -rf` variants (flag transposition, `--force --recursive`), `git push -f`/`--force`, `sudo`, `dd if=`, direct block-device writes. On a match it outputs a deny decision JSON and exits 0; on a safe command it exits 0 silently. Crucially, **hooks fire inside sub-agents and even when `--dangerously-skip-permissions` is active**, so this layer covers both the worker and (as a belt-and-suspenders measure) the planner.

Both files are generated/copied by `agent bootstrap` into `.claude/` of every new project.

**Decision on `--dangerously-skip-permissions` for the worker:**

The flag is **kept** for the worker. Dropping it would require enumerating every Bash command the worker might legitimately need in the settings.json allow list. Workers operate on projects with different stacks (npm, go, cargo, etc.) — an incomplete allow list would cause unexpected permission prompts that hang the unattended loop. The hook + deny list provide the dangerous-pattern blocking layer regardless of the flag; that is what matters for safety. Dropping the flag entirely is deferred to Phase 4 alongside Option 2 (Anthropic's `sandbox-runtime`), which will provide process-level restrictions rather than relying on the allow list for coverage.

For the **planner**: `--dangerously-skip-permissions` was already replaced by `--allowedTools Read,Glob,Grep` in the initial Phase 1.1 work. The settings.json allow list is irrelevant for the planner since `Read`, `Glob`, and `Grep` do not trigger permission prompts. The hook applies as belt-and-suspenders but the planner has no Bash access to exploit.

## Consequences

- The planner no longer receives `--dangerously-skip-permissions`; it only gets `Read`, `Glob`, `Grep`. Any accidental Bash invocation in the planner will now be refused by the CLI.
- Worker runs in a worktree. Files it creates or modifies are isolated from the main working tree until the branch is merged (Phase 3).
- Status.md writes (from the worker) go to the original project root via absolute path, not into the worktree. This keeps the work log in one place and allows the loop's hash check to detect updates.
- `AgentDriver.run()` gains a `cwd: Path | None = None` parameter. Existing callers that omit `cwd` are unaffected.
- Worker sandboxing is still workspace isolation, not security isolation — the worker has full host permissions once `--dangerously-skip-permissions` is granted. The hook + deny list block the most dangerous patterns, but they are not a substitute for process-level restriction. True security isolation is deferred to Phase 4 per ADR-0007.
- `.claude/settings.json` and `.claude/hooks/block-destructive.sh` are generated by `agent bootstrap` into every new project. The hook script is copied from the harness repo so it stays in sync when updated there.
- `AGENT_SANDBOX=noop` disables worktree isolation (useful for bootstrapped projects not under git, or for testing).

## Trigger for moving to Option 2

Before FileGate (unattended runs) ships in Phase 4 — per the recommendation in the Sandboxing PRD.

## References

- `/Users/nils/Documents/nils/PRD/Sandboxing.md` — option analysis and recommendation
- ADR-0007 — worker container/VM sandboxing deferred to Phase 4 (still applies; this ADR addresses Phase 1.1 only)
- ADR-0008 — per-task implementation (Phase 1.2); sandbox wraps the per-task worker loop
