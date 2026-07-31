# Work Log

Session-to-session progress log. Distinct from AGENTS.md (which holds stable rules/conventions).

---

## 2026-07-30 — Phase 1 scaffold

### Done
- Designed layered architecture: drivers, gates, agents, sensors, bootstrap
- Decision: Python runtime, CLI subprocess first, abstracted behind AgentDriver
- Decision: ApprovalGate abstraction; InteractiveGate shipped in Phase 1
- Scaffolded directory structure
- Implemented AgentDriver ABC + ClaudeDriver
- Implemented ApprovalGate ABC + InteractiveGate
- Wrote planner sub-agent definition (agents/planner.md)
- Wrote bootstrap script (bootstrap/bootstrap.py)
- Wrote AGENTS.md for the harness repo
- Implemented runner/loop.py — Phase 1 plan → approve → implement cycle
- Fixed ClaudeDriver.run_subagent: composes agent system prompt inline (no --agent flag); portable across tools
- Added pyproject.toml (setuptools.build_meta backend) + cli.py entry point → `agent bootstrap` / `agent loop`
- Added mise.toml: pins Python 3.12, `mise run install` task; installed and verified via `mise exec -- agent --help`
- Trial run on ev-decide (TypeScript): planner produced a good plan, worker added 33 vitest+fast-check tests, all passing
- Added feedback option (f) to InteractiveGate — amends plan.md before approval, loop redisplays for final decision (ADR-0006)
- Added CLAUDE.md to bootstrap output — bridges interactive Claude Code sessions to AGENTS.md and memory/status.md
- Lesson: stable preferences (test style) belong in AGENTS.md feedforward; one-off amendments go via gate feedback

### Deferred / backlog
- **FileGate** (runner/gates/file.py) — polls for `plan.approved` sentinel file; needed for unattended Ralph loop (Phase 4). Interface already defined in ApprovalGate ABC.
- **GeminiDriver** (runner/drivers/gemini.py) — swap via `AGENT_TOOL=gemini`. Driver interface already defined.
- Phase 2: sensors/ — lint.sh, test.sh, lsp.sh, coverage enforcement
- Phase 3: git/PR automation, error recovery templates
- Phase 4: Ralph loop (loop.py), model router, adversarial review, memory compaction

### Open questions
- Single-player only for now — multi-engineer versioning deferred
- Which LSP(s) to wire up first in Phase 2?

---

## 2026-07-31 — code review findings addressed

### Done
- Sandboxing: removed Bash from planner tool list (read-only enforced at tool level, not just by prompt)
- Sandboxing: wrote ADR-0007 deferring worker container sandboxing to Phase 4; marked FileGate + Ralph loop as blocked by it
- Tests: added 32 table-driven pytest tests for runner/drivers, runner/gates, runner/loop — all passing
- Tests: added pytest as dev dependency; `mise run test` task
- Status check: loop now hashes status.md before worker runs and warns if unchanged after
- Roadmap: sandboxing rows added, Phase 4 blocking dependencies made explicit
- Backlink from roadmap.md to Obsidian PRD added

## 2026-07-31 — Phase 1.2: per-task implementation

### Done
- Planner ## Approach replaced with ## Tasks: INVEST-sized numbered list with title, Files, What
- _parse_tasks() extracts numbered task list from plan.md's ## Tasks section
- _task_title() helper for display
- Loop now implements one task at a time; stops and surfaces on failure at task N
- Per-task status.md hash check (one warning per task, not one per loop run)
- Phase 2 sensor hook point and Phase 3 commit hook point stubbed per task
- ADR-0008 written; roadmap updated with Phase 1.2 between Phase 1 and Phase 2
- 43 tests passing (up from 32): added _parse_tasks, _task_title, per-task execution, mid-task failure, per-task status check

## 2026-07-31 — Phase 1.1 follow-up: permission boundary

### Done
- SandboxRuntime ABC + WorkspaceHandle(path, branch, keep()) in runner/sandbox/base.py
- GitWorktreeSandbox: disposable branch + temp worktree per worker run; branch kept on success, discarded on failure
- NoopSandbox: pass-through for tests and no-git projects
- get_sandbox() factory via AGENT_SANDBOX env var (default: worktree)
- ClaudeDriver.run_subagent: reads tools: frontmatter; uses --allowedTools for planner, --dangerously-skip-permissions only for agents without declared tools
- AgentDriver.run() + ClaudeDriver.run(): added cwd: Path | None = None parameter
- loop.py: uses sandbox context manager around per-task worker loop; absolute paths for context files; handle.keep() on success
- ADR-0009 written; roadmap Phase 1.1 inserted between Phase 1 and Phase 1.2
- AGENTS.md: updated architecture table, design rules, gotchas
- 69 tests passing (up from 43): 16 new sandbox tests, updated driver + loop tests

## 2026-07-31 — Phase 1.1 follow-up: permission boundary

### Done
- .claude/settings.json: permissions allow (pytest, git add/commit/status/diff/log, mise run) + deny (rm -rf, git push, curl, pip install, secrets) + hook registration
- .claude/hooks/block-destructive.sh: PreToolUse Python script; blocks rm -rf variants, git push --force, sudo, dd if=, block-device writes; fires even with --dangerously-skip-permissions
- Decision: keep --dangerously-skip-permissions for worker (can't enumerate all project-specific commands in allow list; hook + deny provide the safety layer); documented in ADR-0009
- Bootstrap generates settings.json (language-specific allow additions) + copies hook to every new project; tested with python lang
- ADR-0009 updated with permission-boundary section and --dangerously-skip-permissions rationale
- AGENTS.md gotchas rewritten to reflect three-layer model (tool grant / hook / allow list)

## Session end: 2026-07-30

Phase 1 complete. Trial run on ev-decide passed. Next session: Phase 2 (sensors).
