# AGENTS.md — Agent Harness repo

Conventions for AI agents working on the harness itself.

## What this repo is

A personal agent harness built on top of Claude Code. It is NOT a replacement for Claude Code — it's an outer layer that adds structure, memory, and quality controls.

## Roadmap

Phase plan with status of each item: `docs/roadmap.md`

## Architecture

```
runner/drivers/   AgentDriver ABC + concrete CLI drivers (swap via AGENT_TOOL env var)
runner/gates/     ApprovalGate ABC + concrete gates (swap via AGENT_MODE env var)
runner/sandbox/   SandboxRuntime ABC + concrete backends (swap via AGENT_SANDBOX env var)
agents/           Sub-agent definitions (markdown, mostly tool-agnostic)
sensors/          Shell scripts: lint, test, LSP checks (no model dependency)
bootstrap/        Sets up a new project with harness structure
memory/           AGENTS.md (this file) + status.md (work log)
```

## Design rules

- The loop only imports from `runner/drivers/base.py`, `runner/gates/base.py`, and `runner/sandbox/base.py` — never a concrete class directly
- New drivers, gates, and sandboxes are added behind the factory functions in `__init__.py`; the loop does not change
- Sensors are plain shell scripts — they must run without the harness, from any terminal or CI
- The planner sub-agent is read-only: it never writes code, only plan.md
- The planner's `tools:` frontmatter field controls `--allowedTools`; the worker uses `--dangerously-skip-permissions`

## Adding a new driver

1. Create `runner/drivers/<tool>.py` implementing `AgentDriver`
2. Add a case to `runner/drivers/__init__.py:get_driver()`
3. Update status.md

## Adding a new gate

1. Create `runner/gates/<mode>.py` implementing `ApprovalGate`
2. Add a case to `runner/gates/__init__.py:get_gate()`
3. Update status.md

## Adding a new sandbox backend

1. Create `runner/sandbox/<name>.py` implementing `SandboxRuntime`
2. Add a case to `runner/sandbox/__init__.py:get_sandbox()`
3. Update status.md

## Testing conventions

- Table-driven tests preferred
- Tests live next to the code they test (`test_<module>.py`)
- Do not delete tests to make coverage pass

## ADR conventions

- Short and sharp: ~20-30 lines total (`docs/arch/adr/0001-python-as-harness-runtime.md`
  is the target shape). If a draft runs past ~40 lines, cut it, don't scope-expand it.
- **Context**: one short paragraph — the problem/constraint, not a transcript of the
  design discussion that led to the decision.
- **Decision**: a few lines, imperative mood. The choice itself, not the reasoning
  (that's Context) or the alternatives (mention a rejected one in a single line only
  if it stops someone from re-litigating it later).
- **Consequences**: a bullet list, one line each. Trade-offs accepted, not restated
  rationale.
- ADRs 0009 and 0011 are known outliers (80+ lines) — don't use them as the template.

## Gotchas

- **`--dangerously-skip-permissions` is kept for the worker** (full tool access required; the allow list can't enumerate every project-specific command). The planner uses `--allowedTools Read,Glob,Grep` instead — no Bash, enforced at the CLI level. Do not conflate the two.
- **The hook fires regardless of `--dangerously-skip-permissions`**. `.claude/hooks/block-destructive.sh` (PreToolUse) blocks `rm -rf`, `git push --force`, `sudo`, etc. even when the flag is set. Updating the hook is the right place to tighten dangerous-command restrictions.
- **`.claude/settings.json` is the allow/deny list** for expected non-interactive commands. If the worker tries a command not in `allow` and not in `deny`, Claude will prompt — which hangs an unattended run. Add expected commands to `allow` when a new stack is introduced.
- The planner agent must output `PLAN READY — awaiting approval.` as its final line; the loop checks for this string.
- Worker runs inside a git worktree (separate directory, same repo). Context files are passed as absolute paths. Status.md writes go to the project root, not the worktree.
