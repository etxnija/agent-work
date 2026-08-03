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
| Metrics — track pass/fail, token cost per loop run | pending |

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

## Phase 3 — Feedback flywheel

Goal: the loop learns across sessions and recovers from known failure modes without human intervention.

| Item | Status |
|---|---|
| Work log (memory/status.md) written by worker after each run | ✅ (basic — from Phase 1) |
| External memory (AGENTS.md updated by worker with discovered patterns) | pending |
| Error recovery templates for recurring failure modes | pending |
| Per-task git commit in worktree branch | ✅ |
| Interactive merge prompt after successful run (y/n fast-forward into main) | ✅ |
| PR creation for review | pending |
| **Keep the branch on task failure instead of `git branch -D`** — today any failed task discards every already-completed task's commits on that run, not just the failing one (`runner/sandbox/worktree.py`) | pending |
| `agent loop --resume` — detect an existing `agent/*` branch with completed task commits, skip them, continue from the first incomplete task | pending — blocked by the item above |
| `agent loop --plan <path>` (or auto-detected prompt) — approve/execute an existing `plan.md` without re-running the planner | pending |

---

## Phase 4 — Tracing & sub-agents

Goal: scale up autonomy — overnight runs, model routing, adversarial review.

| Item | Status |
|---|---|
| Worker sandboxing (container/VM) — **required before FileGate and Ralph loop** | pending |
| FileGate (headless approval via sentinel file) | pending — blocked by sandboxing |
| Ralph loop (iterative: pick task → implement → validate → commit → reset → repeat) | pending — blocked by sandboxing |
| Memory compaction (summarise status.md when it grows large) | pending |
| Model router (select model by task; local-hosted subagents) | pending |
| Insights of model thinking (surface chain-of-thought) | pending |
| Adversarial review (second agent critiques before accepting) | pending |
| Versioned upgrade path (multi-engineer) | pending |

---

## Open questions

- Multi-engineer versioning: single-player for now; design in from Phase 4 or later?
- Model router: custom routing logic or existing framework?
- Local model hosting: in scope for Phase 4 or later?
