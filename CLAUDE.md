# CLAUDE.md

At the start of every session, read these two files before doing anything else:

- **AGENTS.md** — project conventions, stack, testing rules, and gotchas
- **memory/status.md** — session-to-session work log; what has been done and what is next

## Agent harness

This project is managed with the agent harness. Tasks are run via:

```bash
agent loop "task description"
```

The planner sub-agent explores the codebase and writes a plan for human approval before any code is written. Plans are stored in `plan.md`.
