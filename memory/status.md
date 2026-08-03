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

## 2026-07-31 — README task 1: Why section

### Done
- Created README.md with motivation paragraph (problem + feedforward/sensor/flywheel solution) and architecture diagram matching AGENTS.md.
- Added ## Installation section to README.md: prereqs, three-command code block (clone, mise run install, agent --help), and editable-install note.
- Added ## Usage section to README.md: bootstrap subsection (command + what it creates) and loop subsection (command + three-step plan/approve/implement flow with y/n/f options).
- Added ## Further reading section to README.md with links to docs/roadmap.md and docs/arch/adr/, each with a one-line description.
- Created README.md with Why section (3-sentence motivation: problem + feedforward/sandboxing/flywheel solution) and architecture diagram from AGENTS.md.
- Added ## Installation section to README.md: Python 3.12 + mise prereqs, three-step code block (clone, mise run install, agent --help), editable-install note.
- Added ## Usage section to README.md: bootstrap subsection (command + what it creates) and loop subsection (three-step plan/approve/implement flow with y/n/f feedback option).
- Added ## Further reading section to README.md with bullet links to docs/roadmap.md and docs/arch/adr/ with one-line descriptions.

## 2026-08-03 — Phase 2 design: sensors, LSP, and preset architecture

### Done
- Designed Phase 2 sensor-retry mechanism: generic `_run_sensors()` (globs `sensors/*.sh`, no per-sensor-type logic) wired into `run_loop()` with a capped corrective-retry before commit, failing closed (no commit) if retries exhaust — same outcome as a hard worker failure today.
- Decision: LSP sensor uses each language's one-shot batch-diagnostic CLI mode (`pyright --outputjson` etc.) rather than a persistent LSP client/JSON-RPC session — same diagnostic engine as the editor's language server, stays a plain standalone shell script, no new protocol layer in runner/.
- Planner wrote plan.md for Phase 2 (9 tasks); scoped down to Python-only per direction (dropped go/typescript `lsp.sh` templates and settings entries — deferred, see plan.md Out of scope). Plan awaiting human approval/implementation via `agent loop`.
- Separate discussion: how sensor defaults should scale across languages/stacks without repeatedly editing bootstrap.py. Considered and rejected symlinking project sensors to canonical scripts in the agent-work checkout (breaks portability/CI — violates "sensors run standalone" rule). Decided: move `SENSORS` dict content from bootstrap.py Python code to `presets/<lang>/*.sh` data files; bootstrap still copies (not links) into new projects; add bootstrap-time per-sensor command override (preset pre-filled as default); add explicit `agent sensors sync` command later to pull updated presets into existing projects with review (dotfile-manager pattern, not automatic/silent).
- Wrote ADR-0011 (sensor presets as data, not symlinks or per-language code). Added roadmap Phase 2.1 (sensor presets & sync) between Phase 2 and Phase 3. Resolved and removed the "which LSP(s) to wire up first" open question (answered: pyright, Python only, for now).
- Note: ADR-0010 (sensor-retry mechanism + batch-CLI decision) is referenced in plan.md task 9 but not yet written — created only once that plan is implemented.
- Found a real gap while discussing how to (re-)run `agent loop` against the hand-refined plan.md: the planner always overwrites plan.md (no resume/reuse mode), and worse, `runner/sandbox/worktree.py`'s cleanup unconditionally does `git branch -D` on any task failure — discarding every already-completed task's commits from that run, not just the failing one. Logged to roadmap Phase 3: keep branch on failure, `agent loop --resume`, `agent loop --plan <path>`. Not fixed yet — Phase 2 plan.md will be run as-is first.
- Plan Phase 2 task 1: added `_run_sensors(cwd)` helper to runner/loop.py — globs `sensors/*.sh` sorted, runs each via `sh`, returns `(name, combined_output)` for non-zero exits only; not yet wired into `run_loop()`.
- Plan Phase 2 task 2: added `TestRunSensors` table-driven tests to runner/test_loop.py (all-pass, single-fail, multi-fail-sorted, no-sensors-dir) using real throwaway `.sh` scripts under `tmp_path`; 81 tests passing.
- Plan Phase 2 task 3: added `SENSOR_RETRY_LIMIT = 2` and wired `_run_sensors()` into `run_loop()` — replaced the Phase 2 stub comment with a corrective-retry loop (build failure-derived prompt, re-run `driver.run`, re-check sensors) that fails closed (`return 1`, no commit) if sensors still fail after the retry budget or a corrective `driver.run` call itself errors; 81 tests still passing (no new tests added yet — task 4).
- Plan Phase 2 task 5: added `sensors/lsp.sh` (`pyright --outputjson`, two-line pattern matching `lint.sh`/`test.sh`), chmod 755.
- Plan Phase 2 task 6: added `"lsp.sh"` key to `bootstrap/bootstrap.py`'s `SENSORS` dict for `python` (`pyright --outputjson`) and `""` (unconfigured stub); `go`/`typescript` left unchanged.
- Plan Phase 2 task 7: added `"Bash(pyright*)"` to `_SETTINGS_ALLOW_LANG["python"]` in `bootstrap/bootstrap.py` and to this repo's own `.claude/settings.json`; left the pre-existing missing `Bash(ruff*)` entry in `.claude/settings.json` un-backfilled per the plan's "flagged, not mandated" note.
- Plan Phase 2 task 8: flipped `sensors/lint.sh`, `sensors/test.sh`, and the LSP feedback row in `docs/roadmap.md`'s Phase 2 table from pending to ✅; coverage enforcement and metrics rows left pending (out of scope).
- Plan Phase 2 task 9: wrote ADR-0010 (sensor-retry mechanism + batch-CLI vs. persistent-LSP-client decision), ~28 lines, cross-referencing ADR-0008.
