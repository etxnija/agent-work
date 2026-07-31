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
agents/           Sub-agent definitions (markdown, mostly tool-agnostic)
sensors/          Shell scripts: lint, test, LSP checks (no model dependency)
bootstrap/        Sets up a new project with harness structure
memory/           AGENTS.md (this file) + status.md (work log)
```

## Design rules

- The loop only imports from `runner/drivers/base.py` and `runner/gates/base.py` — never a concrete class directly
- New drivers and gates are added behind the factory functions in `__init__.py`; the loop does not change
- Sensors are plain shell scripts — they must run without the harness, from any terminal or CI
- The planner sub-agent is read-only: it never writes code, only plan.md

## Adding a new driver

1. Create `runner/drivers/<tool>.py` implementing `AgentDriver`
2. Add a case to `runner/drivers/__init__.py:get_driver()`
3. Update status.md

## Adding a new gate

1. Create `runner/gates/<mode>.py` implementing `ApprovalGate`
2. Add a case to `runner/gates/__init__.py:get_gate()`
3. Update status.md

## Testing conventions

- Table-driven tests preferred
- Tests live next to the code they test (`test_<module>.py`)
- Do not delete tests to make coverage pass

## Gotchas

- `--dangerously-skip-permissions` is required for unattended Claude CLI runs; do not remove it from ClaudeDriver
- The planner agent must output `PLAN READY — awaiting approval.` as its final line; the loop checks for this string
