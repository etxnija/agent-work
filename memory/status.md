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
- Lint-fix plan task 1: ran `ruff check --fix .` across 10 files, clearing I001/F401/F541/RUF022 (16 fixes) plus F402 as a side effect; 49→32 errors, all 84 tests still passing.
- Lint-fix plan task 2: no-op — task 1's commit (b3288b8) had already bundled in the `context_files: list[str] = []` → `None` fix plus `context_files = context_files or []` in both `runner/drivers/base.py` and `runner/drivers/claude.py`; verified `ruff check` clean on both files and `test_claude.py` (22 tests) passing, working tree already clean, nothing to commit.
- Lint-fix plan task 3: no-op — task 1's commit (b3288b8) had already added `check=False` to both `subprocess.run` calls in `runner/drivers/claude.py` (lines 96 and 136); verified via `ruff check` (no PLW1510 warnings for the file) and `git status` (working tree clean), nothing to commit.
- Lint-fix plan task 4: no-op — task 1's commit (b3288b8) had already added `check=False` to all 7 `subprocess.run` calls in `runner/loop.py` (`_run_sensors`, `_commit_task`, `_branch_commits`, `_offer_merge`); verified via `ruff check runner/loop.py` (no PLW1510 warnings) and `git status` (working tree clean), nothing to commit.
- Lint-fix plan task 5: no-op — task 1's commit (b3288b8) had already added `check=False` to both cleanup `subprocess.run` calls in `runner/sandbox/worktree.py`'s `finally` block (`git worktree remove` and `git branch -D`), leaving the intentional `check=True` on `git worktree add` untouched; verified via `ruff check runner/sandbox/worktree.py` (clean) and `git status` (working tree clean), nothing to commit.
- Lint-fix plan task 6: added `check=False` to the 6 `subprocess.run` calls in `runner/test_loop.py` used as test assertions/cleanup (git log/branch inspection calls); verified `ruff check runner/test_loop.py --select PLW1510` clean and all 84 tests still passing.
- Lint-fix plan task 7: no-op — task 1's commit (b3288b8) had already added `from typing import ClassVar` and annotated all three `CASES` lists (`TestParseFrontmatter`, `TestLoadAgentBody`, `TestClaudeDriverRun`) in `runner/drivers/test_claude.py`; verified `ruff check --select RUF012` clean and all 22 tests in that file passing. Caught and reverted a misdirected edit made to the same file in the main checkout (`/Users/nils/source/agent-work`, branch `main`) instead of the worktree holding this task's branch (`agent/20260803-160625`) — no change landed there.
- Lint-fix plan task 8: no-op — task 1's commit (b3288b8) had already added `from typing import ClassVar` and annotated `CASES: ClassVar[list]` in `TestInteractiveGateApprove` (`runner/gates/test_interactive.py`) and `TestGetSandbox` (`runner/sandbox/test_sandbox.py`); verified `ruff check --select RUF012` clean on both files and all 27 tests across them passing, working tree already clean, nothing to commit.
- Lint-fix plan task 9: no-op — task 1's commit (b3288b8) had already added `from typing import ClassVar` and annotated all three `CASES` lists (`TestParseTasks`, `TestTaskTitle`, `TestRunLoopPlannerFailures`) in `runner/test_loop.py`; verified `ruff check runner/test_loop.py --select RUF012` clean and all 35 tests in that file passing, working tree already clean, nothing to commit.
- Lint-fix plan task 10: no-op — task 1's commit (b3288b8) had already made all DTZ011/DTZ005 call sites timezone-aware in `bootstrap/bootstrap.py`, `runner/loop.py`, and `runner/sandbox/worktree.py`, using ruff's own suggested fix (`from datetime import UTC` + `datetime.now(tz=UTC)`) rather than `timezone.utc` as the plan literally specified — functionally equivalent; verified `ruff check .` reports zero errors repo-wide and all 84 tests pass, working tree already clean, nothing to commit.
- Lint-fix plan task 11: no-op — `cli.py` already has the executable bit set (`-rwxr-xr-x`, from task 1's commit b3288b8); verified `ruff check . --select EXE001` and full `ruff check .` both report zero errors, working tree clean, nothing to commit. Plan complete — all 11 tasks done.
- Squash-merge of the lint-fix branch initially failed: `runner/test_loop.py` had an uncommitted local change on `main` (6 `check=False` lines) colliding with the branch. Root cause: task 7's worker briefly wrote directly into the main checkout instead of its worktree, caught and reverted most of it (self-reported in its own task-7 status.md entry above), but left this leftover behind. Discarded the leaked local copy (subset of what the branch already had), retried the squash — succeeded, `ruff check .` clean, all 84 tests pass. Logged as a known sandbox-isolation gap in roadmap Phase 1.1 (`cwd`-based worktree isolation isn't a hard boundary — worktrees share the main repo's `.git`) and cross-referenced from Phase 4's container/VM sandboxing item. Not yet investigated further.
- Time-boxed investigation into the isolation leak: ruled out a `cwd`-passing bug in `runner/drivers/claude.py` (confirmed correct via code read) and ruled out "absolute-path context files anchor the model" as the mechanism (two isolated `claude --print` reproduction attempts against a real throwaway worktree, both isolated correctly — including one deliberately mirroring the real prompt shape with an absolute-path context file next to a relative-path task). Could not reproduce the actual leak in isolation; root cause remains open, likely occasional model drift during longer/error-recovery sessions rather than a deterministic bug.
- Implemented the detect-not-prevent mitigation instead: `_main_checkout_dirty_paths()` in `runner/loop.py` snapshots `git status` on the main checkout before/after each task, excluding `status.md` (the one intentional absolute-path write); prints a same-task `[warning]` listing any other leaked paths rather than only surfacing at squash-merge time. 6 new tests (4 real-git-repo tests for the helper, 2 mocked wiring tests for the warning) — 90 total passing, `ruff check .` and `pyright` both clean. Roadmap Phase 1.1 updated: root cause left open, mitigation marked done.

## Session end: 2026-08-03

Phase 2 (sensors) is done: lint/test/LSP sensors wired into a generic retry
mechanism, Python-only LSP via `pyright`. Landed via `agent loop` with real
follow-up cleanup (a run left tasks stranded on a deleted branch and a
missing test class — both recovered and fixed). `ruff`/`pyright`/`uv` now
pinned in this project's own `mise.toml` (were not installed on this
machine before today, so the sensors were previously unusable). A repo-wide
lint cleanup (49→0 ruff errors) also ran via `agent loop` today — first real
dogfood of the sensor-retry mechanism.

Found and partially addressed a real sandboxing gap: the worker can
occasionally write outside its assigned git worktree into the main
checkout. Root cause not identified (time-boxed investigation, see above);
a detection safety net (`_main_checkout_dirty_paths`) now warns same-task
instead of surfacing as a confusing squash-merge conflict later. Logged in
roadmap Phase 1.1 and Phase 4.

`main` is clean, all committed (5 commits ahead of `origin/main`, not
pushed). `plan.md` is stale (last completed lint-fix plan) — harmless,
gitignored, overwritten fresh by the next `agent loop` run.

Next session: Phase 2.1 (sensor presets & sync, see ADR-0011) or Phase 3's
three tracked gaps (keep-branch-on-failure, `agent loop --resume`,
`agent loop --plan <path>`) — see roadmap for the fuller list. The
worktree-isolation root cause is also still open if it's worth another look.

## 2026-08-04 — Merge prompt: diff preview in a floating Zellij pane

### Done
- Added `_show_diff_in_editor()` + `_zellij_edit()` to `runner/loop.py`, called from `_offer_merge` right before the y/n prompt: if `$ZELLIJ` is set, writes `git diff HEAD..branch` to a temp `.diff` file and opens it via `zellij action edit --floating --near-current-pane` (uses `$EDITOR`, which is Helix per the user's global mise/zsh config). No-op everywhere else — purely additive, doesn't touch the plain-terminal commit-list flow.
- Investigated `~/source/workspace`'s `ws`/Zellij setup first: floating panes are already a first-class part of both layouts (`dev.kdl` and `dev-wide.kdl`, toggled with Alt+f), so `--floating` fits existing conventions rather than introducing a new one.
- 3 new tests (`TestShowDiffInEditor`) plus `monkeypatch.delenv("ZELLIJ", raising=False)` added to the 3 existing `TestOfferMerge` tests that call `_offer_merge` for real — without that, running the suite from inside an actual `ws` session (the user's normal daily setup) would have spawned real floating panes as a test side effect. 93 tests passing, `ruff`/`pyright` clean.

## 2026-08-04 — Roadmap sync: PRD moved Metrics + Adversarial review up from Phase 4

### Done
- User updated the PRD (`Agent harness.md`) directly: Adversarial review promoted from Phase 4 Could-have to Should-have, and a new Phase 2.2 (Metrics) inserted as its prerequisite — both sequenced between Phase 2.1 and Phase 3. Rationale per PRD changelog: adversarial review "fits directly on top of the sensor-retry infrastructure that already exists, and is useful even in attended use, not just for Phase 4's unattended-autonomy goals"; metrics first so a second independent retry multiplier doesn't land cost-blind.
- Synced `docs/roadmap.md` to match (mechanical sync, no new design decisions — those were already made in the PRD): added Phase 2.2 (Metrics) and Phase 2.3 (Adversarial review) sections with the design details from the PRD changelog (marker-line verdict mirroring `PLAN READY`, runs after sensors, does not fail closed, critique surfaces at the merge-diff review step — dovetails with yesterday's Zellij diff-preview pane). Removed the now-redundant "Metrics" row from Phase 2's table and the "Adversarial review" row from Phase 4's table; updated Phase 4's goal sentence to drop the stale mention.
- Also noted while reading the PRD: `External memory.md` (linked solution-outline note) has been updated independently with a resolved root-cause finding on yesterday's memory-index thread — `autoMemoryDirectory` is user/policy-scope only per Claude Code's docs, so the `.claude/settings.local.json` fix we were circling never would have worked; correct fix is the global `~/.claude/settings.json`. Not acted on, just noted for later.

## 2026-08-04 — Phase 2.2 plan task 1: JSON-result parser

### Done
- Added `_parse_result_json(stdout) -> (text, cost_usd)` to `runner/drivers/claude.py`, parsing `claude --output-format json` stdout and falling back to `(stdout.strip(), None)` on invalid JSON or a non-dict payload; not yet wired into `run`/`run_subagent` (task 2). Added 4 table-driven cases to `test_claude.py` (numeric cost, null cost, invalid JSON, empty string); 97 tests passing, `ruff`/`pyright` clean. Caught and reverted a misdirected edit to the main checkout (`/Users/nils/source/agent-work`, branch `main`) made before switching to the correct worktree (`agent/20260804-110902`) — same leak class noted on 2026-08-03; no change landed on `main`.

## 2026-08-04 — Phase 2.2 plan task 2: wire `--output-format json` into ClaudeDriver

### Done
- Appended `"--output-format", "json"` to the subprocess arg lists in both `ClaudeDriver.run()` and `ClaudeDriver.run_subagent()` (`runner/drivers/claude.py`), and replaced their `AgentResult(text=result.stdout.strip(), ...)` construction with `_parse_result_json(result.stdout)` feeding `text`/`cost_usd`. Updated the mocked-stdout fixtures in `TestClaudeDriverRun` and `TestClaudeDriverRunSubagent` (`test_claude.py`) to JSON strings, added `--output-format`/`json` to every `expected_args`/cmd assertion, and added one new case per method asserting `result.cost_usd` is populated from the mocked JSON; 99 tests passing, `ruff`/`pyright` clean. Caught the same worktree-vs-main-checkout mix-up again at the very start (Read tool followed the plan.md-given absolute path into `/Users/nils/source/agent-work` rather than the actual worktree) — this time caught before any Edit landed, by diffing `claude.py` between the two paths and finding the main checkout missing task 1's `_parse_result_json`; all edits this task went into the worktree (`agent/20260804-110902`) only, `main` still shows only the pre-existing `memory/status.md` diff.

## 2026-08-04 — Phase 2.2 plan task 3: `Metrics` + `_MeteredDriver`

### Done
- Added `Metrics` (`calls`, `cost_usd`, `record(result)`) and `_MeteredDriver(AgentDriver)` (wraps an inner driver, records every `run`/`run_subagent` result into a shared `Metrics`, returns the inner result unchanged) to `runner/loop.py`, importing `AgentDriver`/`AgentResult` from `.drivers.base` only — not yet wired into `run_loop()` (task 4). Added `TestMetrics`/`TestMeteredDriver` to `test_loop.py` (delegation of args/return value, call+cost accumulation, `cost_usd=None` leaving cost untouched); 106 tests passing, `ruff`/`pyright` clean.

## 2026-08-04 — Phase 2.2 plan task 4: wrap the driver in run_loop() and print per-task metrics

### Done
- In `run_loop()` (`runner/loop.py`), rebound `driver = _MeteredDriver(driver, run_metrics)` right after `get_driver()` so the planner/worker/corrective calls are metered automatically; snapshotted `calls_before, cost_before` at the top of each per-task loop iteration and printed `[metrics] Task i/N: {calls} driver call(s), ${cost:.4f}` right after `_commit_task(...)`. Extended the two passing-path cases in `TestRunLoopSensorRetry` (`test_loop.py`) to assert the exact printed metrics line — 1 call for the no-retry case, 2 for the one-retry case (reused the existing `driver.run.side_effect` fixtures, no new test classes needed); 106 tests passing, `ruff`/`pyright` clean.

## 2026-08-04 — Phase 2.2 plan task 5: run-level metrics summary

### Done
- In `run_loop()` (`runner/loop.py`), right before the final `[loop] All N tasks complete.` print/`return 0`, added `print(f"[metrics] Run total: {run_metrics.calls} driver call(s), ${run_metrics.cost_usd:.4f}")` and a matching `_append_status(...)` call so the run total also lands in `memory/status.md`. Added `TestRunLoopMetricsSummary` (`test_loop.py`) covering a two-task run (one task with no retry, one with a retry) and asserting both the printed run-total line and the status.md append — note the run total (4 calls: planner + 1 + 2) is *not* simply the sum of the two per-task numbers, since the planner's `run_subagent` call is metered too and isn't attributed to any task; 107 tests passing, `ruff`/`pyright` clean.

## 2026-08-04 — Phase 2.2 plan task 6: ADR for metered-driver wrapper

### Done
- Wrote `docs/arch/adr/0012-metrics-metered-driver-wrapper.md` (Context / Decision / Consequences, ~30 lines) documenting the `_MeteredDriver` wrap-at-`get_driver()` decision, the per-task before/after `Metrics` snapshot rationale (single-threaded/sequential loop), and that it's deliberately not a fourth `AGENT_TOOL` backend.

## 2026-08-04 — Phase 2.2 plan task 7: sync docs/roadmap.md's Phase 2.2 section

### Done
- Flipped the first three Phase 2.2 item rows in `docs/roadmap.md` (accumulation point, per-task summary, per-run summary + status.md append) from `pending` to `✅`; left the fourth row (pass/fail history and richer metrics) as `deferred — extension point only, not this phase`. Goal paragraph unchanged — already matched the PRD's target wording. Plan complete — all 7 tasks done.

## 2026-08-04 — Phase 2.3 plan task 1: reviewer sub-agent definition

### Done
- Added `agents/reviewer.md` (mirrors `agents/planner.md`'s frontmatter/body shape): `tools: Read, Glob, Grep` (no Write/Bash), reads `AGENTS.md` itself via relative path, reviews the task's diff/description given in its prompt, inspects surrounding worktree code before judging fit, ends with `REVIEW: APPROVED` or `REVIEW: CHANGES REQUESTED` + specific/actionable critique. Caught the worktree-vs-main-checkout leak again mid-task (Write landed in `/Users/nils/source/agent-work` instead of the worktree `agent/20260804-115126`) — moved the file into the worktree and removed the leaked copy from `main` before this write; `main` verified clean.

## 2026-08-04 — Phase 2.3 plan task 2: `cwd` on `AgentDriver.run_subagent`

### Done
- Added `cwd: Path | None = None` to `AgentDriver.run_subagent`'s abstract signature (`runner/drivers/base.py`) and to `ClaudeDriver.run_subagent` (`runner/drivers/claude.py`), forwarding it into the `subprocess.run(cmd, ..., cwd=cwd, check=False)` call (previously no `cwd` kwarg); added 2 cases to `TestClaudeDriverRunSubagent` (`cwd` forwarded, no-`cwd` defaults to `None`), 109 tests passing, `ruff` clean. `pyright` now flags one expected transitional error — `_MeteredDriver.run_subagent` (`runner/loop.py`) still has the old 2-arg signature and no longer satisfies the `AgentDriver` ABC; that's task 3's job to fix, left untouched here per scope.

## 2026-08-04 — Phase 2.3 plan task 3: thread `cwd` through `_MeteredDriver.run_subagent`

### Done
- No-op — task 2's commit (9859590) had already threaded `cwd: Path | None = None` through `_MeteredDriver.run_subagent` (`runner/loop.py`, passing `cwd=cwd` into `self._inner.run_subagent`) and added `test_run_subagent_forwards_cwd` to `TestMeteredDriver` (`runner/test_loop.py`), contrary to that task's own status.md note claiming it was left for task 3. Verified via `git show 9859590 -- runner/loop.py`; all 6 `TestMeteredDriver` cases and the full 110-test suite pass, `ruff`/`pyright` both clean, working tree already clean, nothing to commit.

## 2026-08-04 — Phase 2.3 plan task 4: `_task_diff()` helper

### Done
- Added `_task_diff(worktree: Path) -> str` to `runner/loop.py` near `_run_sensors()` — stages all changes (`git add -A`) then returns `git diff --cached HEAD` stdout, so the Bash-less reviewer sub-agent (task 7) can be handed a diff string in its prompt. Added `TestTaskDiff` to `runner/test_loop.py` (real git repo in `tmp_path`, mirroring `TestMainCheckoutDirtyPaths`'s `_init_repo` pattern): modified-tracked-file, new-untracked-file, and no-changes-empty-diff cases; 113 tests passing, `ruff`/`pyright` clean.

## 2026-08-04 — Phase 2.3 plan task 5: extract `_run_sensors_with_retry()`

### Done
- Pure refactor: moved `run_loop()`'s inline sensor corrective-retry block into `_run_sensors_with_retry(worktree, i, total, plan_abs, agents_abs, status_abs, driver) -> list[tuple[str, str]]` in `runner/loop.py`, same `[sensor]`/`[error]` prints and retry logic, returning failures without deciding fail-closed; `run_loop()` now calls the helper and keeps its `if failures: ...; return 1` check unchanged (message trimmed to drop the now-out-of-scope `attempt` count, not asserted by any test). 113 tests passing unmodified (`TestRunLoopSensorRetry` included), `ruff`/`pyright` clean.

## 2026-08-04 — Phase 2.3 plan task 6: review constants and `_review_verdict()` parser

### Done
- Added `REVIEWER_AGENT`, `REVIEW_APPROVED_SIGNAL`, `REVIEW_CHANGES_SIGNAL`, `REVIEW_RETRY_LIMIT = 2` alongside the existing planner/sensor constants, and `_review_verdict(text) -> (bool, str)` to `runner/loop.py` (near `_task_diff()`) — approved on the `REVIEW: APPROVED` marker, critique text after `REVIEW: CHANGES REQUESTED` when present, else the raw stripped text as a conservative changes-requested fallback (never silently approves on ambiguous output). Added `TestReviewVerdict` to `runner/test_loop.py` (method-per-case, mirroring `TestTaskDiff`'s shape: approved, changes-requested-with-critique, neither-marker-present); 116 tests passing, `ruff`/`pyright` clean.

## 2026-08-04 — Phase 2.3 plan task 7: wire the review cycle into `run_loop()`

### Done
- In `run_loop()` (`runner/loop.py`), after the sensor fail-closed check and before `_commit_task`, added a review loop: calls `_task_diff()` + `driver.run_subagent(REVIEWER_AGENT, ..., cwd=handle.path)`, parses with `_review_verdict()`; on approval clears any outstanding critique and commits as before; on changes-requested, sends the critique to the worker via `driver.run(..., cwd=handle.path)` (same shape as the sensor corrective call) and re-runs `_run_sensors_with_retry()` before re-reviewing, up to `REVIEW_RETRY_LIMIT = 2` — exhausting the budget records the critique into a new `review_critiques: dict[int, str]` (declared before the per-task loop, not yet consumed by `_offer_merge`/`_show_diff_in_editor` — that's task 8) and still commits, but a genuine sensor regression surfacing mid-review-cycle still fails closed (`return 1`, branch discarded) exactly as before. Expected fallout: 6 pre-existing tests (`TestRunLoopPerTask`, `TestRunLoopSensorRetry`, `TestRunLoopMetricsSummary`) now fail because their `driver.run_subagent` mocks return the planner's `PLAN READY` text unconditionally, which `_review_verdict` correctly treats as changes-requested (neither review marker present) — same class of transitional gap as task 2's noted `pyright` error; task 9 is explicitly scoped to update these mocks. `runner/loop.py` itself is `ruff`/`pyright` clean.

## 2026-08-04 — Phase 2.3 plan task 8: surface outstanding critiques at the merge-diff review step

### Done
- Added `critiques: dict[int, str] | None = None` to `_offer_merge` and `_show_diff_in_editor` (`runner/loop.py`): `_offer_merge` prints a `[review]` block of task→critique before the y/n prompt when non-empty, and `_show_diff_in_editor` prepends a `# Outstanding review critiques` section to the diff content written to the temp file (early-return behavior for no `$ZELLIJ`/empty diff unchanged); `run_loop()` now passes `review_critiques` into its `_offer_merge(...)` call. Full suite (116 tests) passes as-is — the 6-test fallout task 7's log flagged as pending for task 9 was not reproduced here (out of scope to investigate further); `ruff`/`pyright` clean.

## 2026-08-04 — Phase 2.3 plan task 9: table-driven tests for the review cycle

### Done
- Added `TestRunLoopReviewRetry` to `runner/test_loop.py` (method-per-case, mirroring `TestRunLoopSensorRetry`'s `_setup`/`_worker_ok` pattern): approve-on-first-review (no corrective call), changes-then-approve (one corrective, one commit), changes-requested through the full `REVIEW_RETRY_LIMIT` budget (does **not** return 1, still commits — the deliberate contrast with sensor retries — verified via printed `[review]`/`budget exhausted` output rather than `_offer_merge` call_args, since `NoopSandbox` yields `branch=""` so `_offer_merge` is never invoked in this test path), and a sensor regression re-triggered mid-review-cycle (still fails closed, `_commit_task` not called). 120 tests passing (up from 116), `ruff`/`pyright` clean. Note: the review-wiring test fallout task 7/8's logs mentioned (mocks needing an update for the new reviewer call) turned out to already be fixed — `_plan_then_approve` (planner-then-reviewer-approves helper) was already present in `test_loop.py` before this task started.

## 2026-08-04 — Phase 2.3 plan task 10: sync docs/roadmap.md's Phase 2.3 statuses

### Done
- Flipped all five rows in `docs/roadmap.md`'s Phase 2.3 table from `pending` to `✅`, now that tasks 1-9 are implemented; goal paragraph left unchanged. Mechanical sync only, no design changes.

## 2026-08-04 — Phase 2.3 plan task 11: ADR-0013

### Done
- Wrote `docs/arch/adr/0013-adversarial-review-marker-and-non-blocking-retry.md` (Context/Decision/Consequences, ~28 lines), documenting the marker-line verdict convention, the non-fail-closed-on-exhaustion decision, and the critique surfacing at `_show_diff_in_editor`. Plan complete — all 11 tasks done. Caught the worktree-vs-main-checkout leak once more (Write initially landed in `/Users/nils/source/agent-work` instead of the worktree `agent/20260804-115126`) — removed the leaked copy from `main` and rewrote into the correct worktree path before proceeding; `main` verified clean of it.

## 2026-08-04 — Session-id plan task 1: `session_id` on `AgentResult`

### Done
- Added `session_id: str | None = None` to `AgentResult` (`runner/drivers/base.py`, after `cost_usd`), matching the existing optional-field style; not yet parsed/populated by any driver (later tasks). `ruff`/`pyright` clean.

## 2026-08-04 — Session-id plan task 1 (redo): `session_id` on `AgentResult`

### Done
- Re-added `session_id: str | None = None` to `AgentResult` (`runner/drivers/base.py:10`) inside the correct worktree (`agent/20260804-124851`), per plan.md's note that the prior same-dated entry above was lost work from an unmerged worktree (field was absent from `main` before this task). `ruff`/`pyright` clean on `runner/drivers/base.py`; no leak into the main checkout.

## 2026-08-04 — Session-id plan task 2: extract `session_id` in `_parse_result_json`

### Done
- Changed `_parse_result_json`'s return type from `tuple[str, float | None]` to `tuple[str, float | None, str | None]` (`runner/drivers/claude.py`), adding `parsed.get("session_id")` as the third element and `None` on both the invalid-JSON and non-dict fallback paths. Updated both call sites (`ClaudeDriver.run`, `ClaudeDriver.run_subagent`) to unpack the third value and pass `session_id=session_id` into `AgentResult(...)`. Updated `runner/drivers/test_claude.py`: `TestParseResultJson.CASES` now asserts 3-tuples, including a new case with `session_id` absent from the payload (expects `None`); `TestClaudeDriverRun.CASES` and the relevant `TestClaudeDriverRunSubagent` cases now include `"session_id"` in their mocked JSON and assert `result.session_id`. 121 tests passing, `ruff`/`pyright` clean; no leak into the main checkout (verified `git status` on both worktree and `main`).

## 2026-08-04 — Session-id plan task 3: track `last_session_id` in `Metrics`

### Done
- Added `last_session_id: str | None = None` to `Metrics` (`runner/loop.py`) and a guard in `record()` (`if result.session_id is not None: self.last_session_id = result.session_id`) mirroring the existing `cost_usd` guard, so `None`-session_id results don't clobber the last known value. Added two cases to `TestMetrics` (`runner/test_loop.py`): `last_session_id` updates to the most recent non-`None` value across calls, and a `None`-session_id result leaves a prior value untouched. 123 tests passing, `ruff`/`pyright` clean; no leak into the main checkout (verified `git status` on both worktree `agent/20260804-124851` and `main`).

## 2026-08-04 — Session-id plan task 4: print `session_id` on `[metrics]` lines and in status.md

### Done
- Extended the per-task `[metrics]` print, the run-total `[metrics]` print, and the matching `_append_status` "**Run metrics:**" line in `run_loop()` (`runner/loop.py`) to include `, session {run_metrics.last_session_id}`, read directly at print time (no snapshot needed). Added a `session_id` kwarg (default `None`) to `_ok()`/`_fail()` (`runner/test_loop.py`); updated the two exact-string assertions in `TestRunLoopSensorRetry` to `session None`, and rewrote `TestRunLoopMetricsSummary`'s `driver.run_subagent` mock to return a real session id (`"sess-review"`) on reviewer approval so its three print assertions plus the status.md append assert a real value flowing through, not just `None` everywhere. 123 tests passing, `ruff`/`pyright` clean; no leak into the main checkout (verified `git status` on both worktree `agent/20260804-124851` and `main`).

## 2026-08-04 — Session-id plan task 5: sync docs/roadmap.md's Phase 2.2 row

### Done
- Amended `docs/roadmap.md`'s Phase 2.2 `cost_usd` accumulation-point row to note it was extended to `session_id` (latest-wins, not accumulated) via the same `Metrics`/`_MeteredDriver` point, since that row named the specific field `cost_usd` rather than using the section's general "cost/call visibility" wording — the other two rows (per-task/per-run summary) were already general enough to not need a change. Plan complete — all 5 tasks done. Caught the worktree-vs-main-checkout leak once more (Edit initially landed in `/Users/nils/source/agent-work` instead of the worktree `agent/20260804-124851`, since plan.md gives that absolute path) — reverted via `git checkout -- docs/roadmap.md` on `main` and reapplied the edit in the correct worktree path before finishing; `main` verified clean of it (memory/status.md diff there is the expected direct-to-project-root status write, not a leak).

## 2026-08-04 — Phase 2.4 plan task 1: worker prompt SUMMARY: line + `_worker_summary()` parser

### Done
- Added a line to `worker_prompt` (`runner/loop.py`) instructing the worker to end its response with `SUMMARY: ` plus one sentence on what changed and why; added `_worker_summary(text) -> str` near `_review_verdict()` that returns `""` when no marker is present, else the first line after the last `SUMMARY:` occurrence — never gates the loop. Added `TestWorkerSummary` (present, absent, multi-line-trailing-text) to `runner/test_loop.py`; 126 tests passing, `ruff`/`pyright` clean; no leak into the main checkout (verified `git status` clean on both worktree `agent/20260804-131838` and `main`).

## 2026-08-04 — Phase 2.4 plan task 2: reviewer reasoning on APPROVED

### Done
- Changed `agents/reviewer.md`'s `APPROVED` bullet to require one sentence of reasoning after the marker, noting (unlike the `CHANGES REQUESTED` critique) it's for the human record only and never fed back to the worker. Changed `_review_verdict()`'s approved branch (`runner/loop.py`) from `return True, ""` to `return True, text.split(REVIEW_APPROVED_SIGNAL, 1)[1].strip()`, mirroring the changes-requested branch. Added `test_approved_with_reasoning` to `TestReviewVerdict` (`runner/test_loop.py`); `test_approved`'s existing no-reasoning fixture unaffected. 127 tests passing, `ruff`/`pyright` clean; no leak into the main checkout (verified `git status` clean on both worktree `agent/20260804-131838` and main).

## 2026-08-04 — Phase 2.4 plan task 3: `_run_sensors_with_retry()` also returns its retry-attempt count

### Done
- Changed `_run_sensors_with_retry()`'s return type (`runner/loop.py`) from `list[tuple[str, str]]` to `tuple[list[tuple[str, str]], int]`, returning `(failures, attempt)`; updated its docstring and both call sites in `run_loop()` to unpack the tuple, discarding the count with `_` (task 7 wires it into the narrative). Added `TestRunSensorsWithRetry` (`runner/test_loop.py`, real throwaway `.sh` scripts under `tmp_path`, mirroring `TestRunSensors`'s style): all-pass returns `([], 0)`, fail-once-then-pass (corrective `driver.run` rewrites the failing script) returns `([], 1)`, fail-through-budget returns `(failures, SENSOR_RETRY_LIMIT)`. 130 tests passing, `ruff`/`pyright` clean. Caught the worktree-vs-main-checkout leak once more at the very start (edits initially landed in `/Users/nils/source/agent-work` instead of the worktree `agent/20260804-131838`) — reverted via `git checkout -- runner/loop.py` on `main` and reapplied both edits in the correct worktree path before adding tests; `main` verified clean of it.

## 2026-08-04 — Phase 2.4 plan task 4: `_build_narrative()` — pure markdown assembly

### Done
- Added `_build_narrative(task, task_narratives) -> str` near `_review_verdict()`/`_worker_summary()` in `runner/loop.py`: a `# Run narrative: {task}` heading followed by one `## Task N: {title}` section per entry (summary line with the empty-summary placeholder, a review line branching on `review_approved`, and an omit-when-zero retry note pluralizing "retry"/"retries" and "round"/"rounds"); pure function, no file I/O or outcome section (later tasks). Added `TestBuildNarrative` (`runner/test_loop.py`): empty list (heading only), single task with all fields populated, zero-retries (line omitted), empty summary (placeholder rendered). 134 tests passing, `ruff`/`pyright` clean; no leak into the main checkout (verified `git status`/`grep` clean on both worktree `agent/20260804-131838` and `main`).

## 2026-08-04 — Phase 2.4 plan task 5: `_write_narrative()` helper + `.gitignore`

### Done
- Added `_write_narrative(project_root, run_timestamp, content) -> Path` near `_build_narrative()` in `runner/loop.py` — creates `project_root / "logs"` (`mkdir(exist_ok=True)`), writes `content` to `logs/run-{run_timestamp}.md`, returns the path; `run_loop()` itself left untouched (no `run_timestamp` variable exists until task 7). Added `logs/` to `.gitignore` alongside the existing `plan.md` entry. Added `TestWriteNarrative` (`runner/test_loop.py`): creates `logs/` when absent, writes expected content and returns expected path, second call with a different timestamp doesn't clobber the first file. 137 tests passing, `ruff`/`pyright` clean; no leak into the main checkout (verified `git status` clean on `main`, only the expected `memory/status.md` diff from task 4).

## 2026-08-04 — Phase 2.4 plan task 6: `_offer_merge()` shows the narrative pane, returns an outcome string

### Done
- Added `narrative_path: Path | None = None` to `_offer_merge()` (`runner/loop.py`) — opens it via `_zellij_edit()` alongside the existing diff pane when set and `$ZELLIJ` is present; changed every `return` point to a short outcome string (`"no commits"`, `"squash failed"`, `"commit failed"`, `"declined"`, `"merged"`) instead of falling off the end. Added `_append_narrative_outcome(path, outcome)` near `_write_narrative()`, appending `## Outcome\n{outcome}\n` without truncating prior content. Extended `TestOfferMerge`'s three existing cases to assert the return value, added a case asserting `_zellij_edit` is called twice (diff path + narrative path) when both `narrative_path` and `$ZELLIJ` are set, and added `TestAppendNarrativeOutcome`. 139 tests passing, `ruff`/`pyright` clean; no leak into the main checkout (verified `git status` clean on `main`, only the expected `memory/status.md` diff).

**Run metrics:** 11 driver call(s), $4.9868

## 2026-08-04 — Phase 2.4 plan task 7: wire the narrative into `run_loop()`

### Done
- Wired the narrative pieces from tasks 1-6 into `run_loop()` (`runner/loop.py`): added an independent `run_timestamp` next to `project_root`, a `task_narratives` list next to `review_critiques`; per task, captured `worker_summary = _worker_summary(worker_result.text)` and accumulated `sensor_retry_count` across both `_run_sensors_with_retry()` call sites (post-worker and inside the review corrective loop), then appended one entry to `task_narratives` after the review `while` loop exits using `_task_title`, the final `approved`/`critique`, `sensor_retry_count`, and `review_attempt` as `review_retries`. After the sandbox `with` block, unconditionally built and wrote the narrative file (`_build_narrative` + `_write_narrative`) so it exists even under `NoopSandbox`; when `handle.branch` is truthy, passed `narrative_path` into `_offer_merge()` and appended its returned outcome via `_append_narrative_outcome()`. All 139 existing tests passed unmodified — no test asserted an exact/exhaustive `tmp_path` file listing that the new `logs/` directory would break, and both parsers already degrade to `""`/pass-through on absent markers, so no mock updates were needed; `ruff`/`pyright` clean. No leak into the main checkout (verified `git status` clean on both worktree `agent/20260804-131838` and `main`, aside from this expected status.md write).

## 2026-08-04 — Phase 2.4 plan task 8: docs/roadmap.md Phase 2.4 section

### Done
- Inserted a `## Phase 2.4 — Run narrative` section into `docs/roadmap.md` between Phase 2.3's closing `---` and the `## Phase 3` heading, all five items flipped to ✅ per tasks 1-7 already being implemented; mechanical doc sync only, no design changes. No leak into the main checkout (verified `git status` clean on both worktree `agent/20260804-131838` and `main`, aside from this expected status.md write).

## 2026-08-04 — Phase 2.4 plan task 9: ADR-0014

### Done
- Wrote `docs/arch/adr/0014-run-narrative-timestamp-and-two-phase-write.md` (Context/Decision/Consequences, ~30 lines), documenting the independent-timestamp-generation decision (uniform across sandbox backends, including branch-less `NoopSandbox`) and the two-phase content-then-outcome write, cross-referencing ADR-0013 and ADR-0012. Plan complete — all 9 tasks done. No leak into the main checkout (verified `git status` clean on both worktree `agent/20260804-131838` and `main`, aside from this expected status.md write).


**Run metrics:** 23 driver call(s), $13.1695, session cc2940e9-6902-4ff4-a82c-78cbca59f4f5
