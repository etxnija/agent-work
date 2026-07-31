# agent-work

Claude Code works well interactively but provides no structure for repeated, quality-controlled, unattended runs. Without scaffolding, every agent session starts blank: no feedforward guides, no sandboxing, no record of what worked or failed. This harness adds three things: a planner/gate loop that produces a human-approved plan before any code is written, per-task worktree sandboxing so a failed run leaves no partial state, and a feedback flywheel that accumulates lessons in `memory/status.md` across sessions.

```
runner/drivers/   AgentDriver ABC + concrete CLI drivers (swap via AGENT_TOOL env var)
runner/gates/     ApprovalGate ABC + concrete gates (swap via AGENT_MODE env var)
runner/sandbox/   SandboxRuntime ABC + concrete backends (swap via AGENT_SANDBOX env var)
agents/           Sub-agent definitions (markdown, mostly tool-agnostic)
sensors/          Shell scripts: lint, test, LSP checks (no model dependency)
bootstrap/        Sets up a new project with harness structure
memory/           AGENTS.md (this file) + status.md (work log)
```

## Installation

Prerequisites: Python 3.12, [mise](https://mise.jdx.dev).

```sh
git clone <repo-url> ~/source/agent-work
cd ~/source/agent-work
mise run install
agent --help
```

`mise run install` does an editable install via `pyproject.toml`, so edits to source take effect immediately without reinstalling.
