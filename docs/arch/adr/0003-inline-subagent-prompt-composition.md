# ADR-0003: Inline sub-agent prompt composition

## Status

Accepted

## Context

Sub-agents are defined as markdown files with a YAML header and a system prompt body. The driver needs to deliver that system prompt to the model. Claude Code has a `.claude/agents/` convention; other tools have their own mechanisms or none at all. Using a tool-specific flag (e.g. a hypothetical `--agent` flag) would break every driver that doesn't support it.

## Decision

Each driver reads the agent markdown file, strips the YAML frontmatter, and prepends the body to the user prompt as a single composed string before calling the model CLI. No CLI flags are used for sub-agent invocation.

Context files are similarly injected inline as `<file path="...">...</file>` blocks prepended to the prompt, rather than via CLI flags.

## Consequences

- Agent definitions are tool-agnostic markdown. The same `agents/planner.md` works with any driver.
- Drivers are interchangeable: the composition logic lives in each driver's `run_subagent()`, not in shared infrastructure.
- The composed prompt is slightly longer than a native sub-agent invocation would be. This is acceptable for Phase 1 task sizes.
- If a future tool offers a native sub-agent protocol that is clearly superior, a driver can adopt it internally without changing the interface.
