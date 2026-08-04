# Roadmap

Sequencing follows the harness-engineering pattern: feedforward guides first, then sensors, then feedback flywheel, then tracing and sub-agents. Each phase is usable before the next begins.

Source of truth for requirements: [[PRD/Agent harness]] in the Obsidian vault (`/Users/nils/Documents/nils/PRD/Agent harness.md`).

---

## Phase 1 — Feedforward guides ✅

Goal: a working bootstrap + planner gate. The success criterion is: run a bootstrap command, get a plan approved, implement the first feature.

| Item | Status |
|---|---|
| Bootstrap (`agent bootstrap`) | ✅ |
| Planner sub-agent (read-only + plan.md) | ✅ |
| Approval gate with feedback option (y/n/f) | ✅ |
| `agent loop` CLI (plan → approve → implement) | ✅ |
| CLAUDE.md — bridges interactive sessions to AGENTS.md + status.md | ✅ |
| AgentDriver abstraction (swap tools via AGENT_TOOL) | ✅ |
| ApprovalGate abstraction (swap modes via AGENT_MODE) | ✅ |
| Editable install via `mise run install` | ✅ |
| Tests for runner/drivers, runner/gates, runner/loop (32 passing) | ✅ |
| Sandboxing — planner Bash tool removed (read-only enforced at tool level) | ✅ |
| Sandboxing — worker container/VM isolation | ⏳ deferred to Phase 4 (see ADR-0007) |

Trial run: ev-decide (TypeScript). Planner explored a real codebase; worker added 33 vitest + fast-check tests, all passing.

---

## Phase 1.1 — Sandboxing baseline ✅

Goal: close the concrete sandboxing gap found in code review without new dependencies. See ADR-0009.

| Item | Status |
|---|---|
| `SandboxRuntime` ABC + `get_sandbox()` factory (mirrors AgentDriver/ApprovalGate pattern) | ✅ |
| `GitWorktreeSandbox` — disposable branch + worktree per worker run | ✅ |
| `NoopSandbox` — pass-through for testing / no-git projects | ✅ |
| Planner uses `--allowedTools Read,Glob,Grep` instead of `--dangerously-skip-permissions` | ✅ |
| `ClaudeDriver.run_subagent` reads `tools:` from agent frontmatter to select flag | ✅ |
| Worker context files passed as absolute paths (cwd-independent) | ✅ |
| `.claude/settings.json` — permissions allow/deny list for expected worker commands | ✅ |
| `.claude/hooks/block-destructive.sh` — PreToolUse hook blocking rm -rf, force-push, sudo etc. | ✅ |
| Bootstrap generates settings.json + copies hook into every new project | ✅ |
| Tests: `runner/sandbox/test_sandbox.py` (16 passing); loop + driver tests updated (69 total) | ✅ |
| **Known gap: `cwd`-based worktree isolation is not fully reliable** — observed 2026-08-03: a worker task wrote directly into the main checkout instead of its assigned worktree, caught and reverted most of it, but left an uncommitted leftover that collided with the next squash-merge. Time-boxed investigation (2026-08-03): ruled out a `cwd`-passing bug in `runner/drivers/claude.py` (confirmed correct) and ruled out "absolute-path context anchors the model" as the mechanism (two isolated reproduction attempts, one deliberately mirroring the real prompt shape, both isolated correctly). Could not reproduce the actual leak — it happened inside the sensor-retry corrective-call path on task 7 of an 11-task run; replaying that exact multi-task shape was judged too expensive for the time-box. Likely occasional model drift during longer/error-recovery sessions, not a deterministic mechanism bug. Root cause still open — strengthens the case for Phase 4's container/VM sandboxing as the real fix. | root cause open |
| `_main_checkout_dirty_paths()` — detects (doesn't prevent) the leak above: snapshots `git status` on the main checkout before/after each task, warns same-task if anything besides `status.md` changed, instead of surfacing as a confusing squash-merge conflict several tasks later | ✅ |

---

## Phase 1.2 — Per-task implementation ✅

Goal: plan size, diff size, and PR size are all bounded. Prerequisite for Phase 2 sensor feedback to be locally actionable and for Phase 3/4 commits and PRs to be reviewably sized. See ADR-0008.

| Item | Status |
|---|---|
| Planner `## Tasks` format: numbered INVEST-sized tasks replacing `## Approach` narrative | ✅ |
| `_parse_tasks()` extracts task list from plan.md after approval | ✅ |
| Loop implements one task at a time; stops and surfaces on failure | ✅ |
| Per-task status.md entry and hash check | ✅ |
| Phase 2 sensor hook point per task (stub, wired in Phase 2) | ✅ |
| Phase 3 git commit hook point per task (stub, wired in Phase 3) | ✅ |

---

## Phase 2 — Computational sensors

Goal: the loop gets deterministic feedback on what the worker produces. The worker self-corrects before the human sees the output.

| Item | Status |
|---|---|
| `sensors/lint.sh` — linter runs after worker, output fed back | ✅ |
| `sensors/test.sh` — test suite runs after worker, failures fed back | ✅ |
| LSP feedback (batch-CLI: `pyright`, Python only) — type errors injected into worker context | ✅ |
| 100% test coverage enforced on new code | pending |

---

## Phase 2.1 — Sensor presets & sync

Goal: opinionated sensor defaults (what "clean" means per stack) scale to new
languages and stack variants without editing harness source, and already-bootstrapped
projects can pull updated opinions on demand without losing local overrides. See
ADR-0011.

| Item | Status |
|---|---|
| Presets moved from `bootstrap.py`'s `SENSORS` dict to `presets/<lang>/*.sh` files | pending |
| Bootstrap copies preset files into new project's `sensors/` (still standalone, no runtime harness dependency) | pending |
| Bootstrap prompts for a per-sensor command override, preset pre-filled as default | pending |
| `agent sensors sync` — diff project's `sensors/` against current presets, apply per-file with review | pending |
| Coverage / code-health sensors added as new presets (reuse `_run_sensors()`, no `loop.py` changes) | pending |

---

## Phase 2.2 — Metrics

Goal: driver-call cost and count visible per task and per run, with a single obvious
accumulation point so a second metric (retry counts, wall-clock time) is a small
addition later, not a redesign. Moved up from Phase 4 (PRD, 2026-08-04) — prerequisite
for Phase 2.3, since adversarial review adds a second independent retry multiplier
that shouldn't land without cost visibility.

| Item | Status |
|---|---|
| Track `AgentResult.cost_usd` per driver call, routed through one accumulation point in loop.py — extended to `session_id` (latest-wins, not accumulated) via the same `Metrics`/`_MeteredDriver` point | ✅ |
| Print a per-task metrics summary | ✅ |
| Print a per-run summary and append it to `memory/status.md` | ✅ |
| Pass/fail history and richer metrics (retry counts, wall-clock time) | deferred — extension point only, not this phase |

---

## Phase 2.3 — Adversarial review

Goal: a second, independent agent critiques each task's diff before it's committed —
judgment calls a script can't make, same feedback-retry shape as sensors. Moved up
from Phase 4 Could-have to Should-have (PRD, 2026-08-04): "fits directly on top of
the sensor-retry infrastructure that already exists, and is useful even in attended
use, not just for Phase 4's unattended-autonomy goals." Blocked by Phase 2.2 (cost
visibility).

| Item | Status |
|---|---|
| Reviewer sub-agent runs after sensors pass, before commit | ✅ |
| Verdict signaled via a marker line (mirrors the planner's `PLAN READY` signal), not structured output | ✅ |
| On disagreement: critique fed back to the worker as a corrective prompt, same shape as a sensor failure | ✅ |
| `REVIEW_RETRY_LIMIT` (2) — unlike sensors, does **not** fail closed: commits anyway after the retry budget | ✅ |
| Outstanding critique surfaced at the merge-diff review step (dovetails with the Zellij diff-preview pane) instead of discarding real work over a model disagreement | ✅ |

---

## Phase 2.4 — Run narrative

Goal: a short, human-readable summary of what happened during a run, for
understanding the process rather than decision-gating. Built from one-liners
agents already produce, not a new summarization call.

| Item | Status |
|---|---|
| Worker prompt ends with a one-line SUMMARY: what changed and why | ✅ |
| Reviewer always includes one line of reasoning alongside its verdict marker, not just on CHANGES REQUESTED | ✅ |
| Per-run narrative assembled mechanically from captured one-liners: task, per-task summary + review verdict + retry notes, final outcome | ✅ |
| Written to logs/run-<timestamp>.md, one file per run; logs/ added to .gitignore | ✅ |
| Shown in a floating Zellij pane via _zellij_edit(), alongside the diff pane at merge time | ✅ |

---

## Phase 3 — Feedback flywheel

Goal: the loop learns across sessions and recovers from known failure modes without human intervention.

| Item | Status |
|---|---|
| Work log (memory/status.md) written by worker after each run | ✅ (basic — from Phase 1) |
| External memory (AGENTS.md updated by worker with discovered patterns) | pending |
| Error recovery templates for recurring failure modes | pending |
| Per-task git commit in worktree branch | ✅ |
| Interactive merge prompt after successful run (y/n fast-forward into main) | ✅ |
| Merge prompt opens the branch's diff in a floating Zellij pane (`$EDITOR`) before asking y/n — personal workflow convenience, no-op outside Zellij | ✅ |
| PR creation for review | pending |
| **Keep the branch on task failure instead of `git branch -D`** — today any failed task discards every already-completed task's commits on that run, not just the failing one (`runner/sandbox/worktree.py`) | pending |
| `agent loop --resume` — detect an existing `agent/*` branch with completed task commits, skip them, continue from the first incomplete task | pending — blocked by the item above |
| `agent loop --plan <path>` (or auto-detected prompt) — approve/execute an existing `plan.md` without re-running the planner | pending |

---

## Phase 4 — Tracing & sub-agents

Goal: scale up autonomy — overnight runs, model routing. (Adversarial review moved to
Phase 2.3 — PRD, 2026-08-04.)

| Item | Status |
|---|---|
| Worker sandboxing (container/VM) — **required before FileGate and Ralph loop**; motivated further by the `cwd`-isolation leak found in Phase 1.1 (worktree sharing the main repo's `.git` isn't a hard boundary) | pending |
| FileGate (headless approval via sentinel file) | pending — blocked by sandboxing |
| Ralph loop (iterative: pick task → implement → validate → commit → reset → repeat) | pending — blocked by sandboxing |
| Memory compaction (summarise status.md when it grows large) | pending |
| Model router (select model by task; local-hosted subagents) | pending |
| Insights of model thinking (surface chain-of-thought) | pending |
| Versioned upgrade path (multi-engineer) | pending |

---

## Open questions

- Multi-engineer versioning: single-player for now; design in from Phase 4 or later?
- Model router: custom routing logic or existing framework?
- Local model hosting: in scope for Phase 4 or later?
