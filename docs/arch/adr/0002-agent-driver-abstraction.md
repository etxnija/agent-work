# ADR-0002: Agent tool abstraction via driver interface

## Status

Accepted

## Context

The harness loop needs to call an AI model. Binding directly to a specific tool (Claude CLI, Gemini CLI, SDK) would make the loop impossible to retarget without widespread changes. The primary goal is that the loop and all orchestration logic never import a concrete tool.

## Decision

Define `AgentDriver` as an abstract base class with two methods: `run(prompt, context_files)` and `run_subagent(agent_name, prompt)`. Concrete implementations live in `runner/drivers/`. A factory function `get_driver()` selects the implementation via the `AGENT_TOOL` environment variable. The loop only imports from `base.py` and calls the factory.

`ClaudeDriver` (CLI subprocess) ships first. `GeminiDriver` is deferred.

## Consequences

- Adding a new tool is one new file in `runner/drivers/` and one line in `get_driver()`. No loop code changes.
- Swapping tools in CI or overnight runs is an env var: `AGENT_TOOL=gemini agent loop "..."`.
- The CLI subprocess approach is simpler than the SDK but loses structured output and streaming. An SDK-based driver can be added later behind the same interface without touching the loop.
