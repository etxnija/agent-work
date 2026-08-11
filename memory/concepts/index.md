# Concept Index

Index of durable codebase concepts, patterns, decisions, and components.
Each concept file contains YAML frontmatter metadata (`type`, `tags`, `summary`, `links`, `updated`).

---

- **[sandboxing.md](sandboxing.md)** (`type: decision` | `tags: [sandboxing, worktree, security, permissions]`) — Git worktree isolation and .claude permission rules.
- **[sensors-coverage.md](sensors-coverage.md)** (`type: component` | `tags: [sensors, coverage, lint, pyright]`) — Sensor retry mechanics, type checks, and diff-coverage floor.
- **[metrics.md](metrics.md)** (`type: component` | `tags: [metrics, cost, session, telemetry]`) — Driver call accounting, cost tracking, and session ID recording.
- **[adversarial-review.md](adversarial-review.md)** (`type: pattern` | `tags: [reviewer, review, haiku, clean-slate]`) — Clean-slate Haiku review agent, verdict markers, and non-blocking retry budget.
- **[task-decomposition.md](task-decomposition.md)** (`type: pattern` | `tags: [invest, loop, task, commits]`) — INVEST task sizing, per-task commit hooks, and merge workflow.
