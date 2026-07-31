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

## Usage

### Bootstrap a project

```sh
agent bootstrap <project-dir> --lang go|typescript|python
```

Creates in `<project-dir>`:

- `AGENTS.md` — conventions, stack, and gotchas for agents working in that project
- `memory/status.md` — session-to-session work log
- `sensors/` — placeholder directory for lint, test, and LSP check scripts
- `.claude/settings.json` — allow/deny list for unattended commands
- `.claude/hooks/block-destructive.sh` — PreToolUse hook that blocks `rm -rf`, `git push --force`, `sudo`, and similar even when `--dangerously-skip-permissions` is set

### Run the loop

```sh
agent loop "add a /health endpoint"
```

Three steps:

1. **Planner** — a read-only sub-agent explores the codebase and writes `plan.md`
2. **Approval gate** — you review the plan and respond `y` (approve), `n` (reject), or `f` (give feedback). Feedback amends `plan.md` and the gate loops back for a final decision before any code is written.
3. **Worker** — implements the plan one task at a time inside a disposable git worktree; stops and surfaces the error if any task fails
