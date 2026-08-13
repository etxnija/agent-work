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
| **Fix: plan validation now runs before the approval gate** — `PLAN_READY_SIGNAL` presence and `_parse_tasks()` non-empty are checked with a `PLANNER_RETRY_LIMIT = 2` corrective-retry loop, matching the `SENSOR_RETRY_LIMIT`/`REVIEW_RETRY_LIMIT` pattern from Phase 2, before `gate.request()` is ever called — a human is never asked to approve a plan already known to be unusable. Gap found via independent architecture review. | ✅ |

---

## Phase 2 — Computational sensors

Goal: the loop gets deterministic feedback on what the worker produces. The worker self-corrects before the human sees the output.

| Item | Status |
|---|---|
| `sensors/lint.sh` — linter runs after worker, output fed back | ✅ |
| `sensors/test.sh` — test suite runs after worker, failures fed back | ✅ |
| LSP feedback (batch-CLI: `pyright`, Python only) — type errors injected into worker context | ✅ |
| 100% diff-aware coverage on new/changed code (`diff-cover`) + a whole-repo regression floor vs. main's cached baseline (`coverage.py`) | ✅ |

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

## Phase 2.5 — Sensor cost optimization

Goal: cut wasted compute and noisy corrective prompts in the per-task sensor pass.
Sensors already run cheap-to-expensive (`lint.sh`, `lsp.sh`, `test.sh`); running an
expensive sensor after a cheap one already failed wasted compute and produced
confounded corrective prompts. `ruff check --fix` mechanically resolves lint issues a
model correction would otherwise be spent on.

| Item | Status |
|---|---|
| `_run_sensors()` short-circuits at the first failing sensor instead of running the full sorted set | ✅ |
| `SENSOR_RETRY_LIMIT` stays a single budget shared across serialized failures surfaced one at a time, not reset per sensor | ✅ |
| `sensors/lint.sh` — `ruff check --fix .` auto-fixes safe violations before reporting failure | ✅ |
| `bootstrap.py`'s Python lint preset mirrors the auto-fix change for newly bootstrapped projects | ✅ |

---

## Phase 2.6 — Code-health sensor

Goal: a deterministic, cheap check for length/complexity/duplication that fits the
existing sensor-retry shape, catching what lint/test/type-checks don't. Retry-then-
tolerate (like Review), not fail-closed like the other sensors — a threshold is
inherently a little arbitrary, unlike a lint error.

| Item | Status |
|---|---|
| `runner/code_health.py` — `lizard`-based length/complexity/duplication check on `git diff --name-only main`, file-level scope | ✅ |
| `_run_code_health_with_retry()` — `CODE_HEALTH_RETRY_LIMIT = 2`, retry-then-tolerate, sequenced after sensors (hard), before review (soft) | ✅ |
| Test files excluded from the check entirely — first live run flagged 71 findings, all from the duplicate-detector matching legitimate repetitive test-fixture helpers, which lizard has no concept of being intentional | ✅ |

---

## Phase 2.7 — Token/cost optimization

Goal: cut real, observed per-run token/dollar cost without weakening the sensor/review
gates. Motivated by live cost data from Phase 2.2/2.3 showing individual reviewer
calls running 25+ turns and 700k+ cache-read tokens for a job that should be
read-diff-and-verdict.

| Item | Status |
|---|---|
| `memory/concepts/*.md` — OKF-lite concept bundles (YAML frontmatter: type/tags/summary/links/updated) as durable, tagged, bounded context, replacing the heavyweight `[plan_abs, agents_abs, status_abs]` bundle for worker/corrective/review calls | ✅ |
| `_parse_task_concepts()` — explicit `Concepts:` line in a task, falling back to automated tag-matching against concept frontmatter when omitted | ✅ |
| Prompt caching — static instruction text hoisted into module-level constants, `AGENTS.md` pre-injected via `context_files` ahead of dynamic task/diff content, so repeated prefixes actually cache | ✅ |
| `MAX_DIFF_LINES = 500` — reviewer's diff capped with a disclosed `"[diff truncated: N lines omitted]"` marker, not silent | ✅ |
| `agents/reviewer.md` bounded — explicit division of labor with sensors (don't re-derive mechanical checks), exploration capped at 1-3 targeted Read/Grep calls, excessive exploration reframed as a complexity signal rather than thoroughness | ✅ |
| Real-time `:start`/`:done` progress logging + immediate `sys.stdout.flush()` across the per-task pipeline | ✅ |
| **Planner's own status.md read is still unbounded** — reads the entire file (111KB+ and growing) unconditionally every planning call, on the most expensive model (`claude-opus-4-6`); the one place in the pipeline not yet covered by this optimization pass | pending — blocked was on durable memory currency (now built, not yet verified reliable — see Phase 3) |

---

## Phase 3 — Feedback flywheel

Goal: the loop learns across sessions and recovers from known failure modes without human intervention.

| Item | Status |
|---|---|
| Work log (memory/status.md) written by worker after each run | ✅ (basic — from Phase 1) |
| **Status.md ownership fix (2026-08-12)** — corrected inaccurate documentation (`AGENTS.md` gotcha, `_main_checkout_dirty_paths()` docstring both claimed workers write status.md to the main checkout "by design"; actual behavior is the Worker writes into the worktree, which is the better property — the entry travels with its task's commit, so an abandoned task's narrative is abandoned too). Fixed the one piece that was actually broken: the harness's own two run-level `_append_status()` calls (run-metrics, plan-rejected) left permanent uncommitted dirty state since nothing committed them — now auto-committed immediately via `_commit_status_update()`, scoped strictly to `memory/status.md` so it can never sweep up an unrelated dirty file (e.g. an unreviewed leak). This is what caused a real stash-pop merge conflict earlier the same night. | ✅ |
| **Durable memory currency (AGENTS.md + memory/concepts/\*.md)** — built and merged 2026-08-12: `WORKER_STATIC_INSTRUCTIONS` rule 4, extended so the Worker updates AGENTS.md (convention change) or the matching `memory/concepts/*.md` file (component/pattern/decision change) when a task represents durable knowledge, citing the task summary; new concept files follow the existing frontmatter schema and get added to `memory/concepts/index.md`; `_stamp_verified()` wired into `_perform_squash_merge()` so any concept file in a squash gets a `verified: [{by: "human", at}]` stamp at the moment a human approves the merge. **Reliability unproven**: 0-for-2 on its first live run — two tasks that clearly matched the rule's own criteria (adding `_stamp_verified()`; wiring it into the merge path) triggered no concept-file update at all. Needs a deliberate test task designed to trigger it before being trusted, and/or a stronger instruction. | ✅ built, not yet verified reliable |
| **Worker status.md write-location is inconsistent within a single run** — confirmed directly (2026-08-12): of 8 tasks in one run, 7 wrote their status.md entry into the worktree (correct/expected), 1 wrote directly to the main checkout instead, with no path given to explain the difference — same instruction, same session shape. Blocked that run's own squash-merge. Not yet root-caused or fixed. | root cause open |
| **Review-warning timing is misleading** — `[warning] Worker did not update memory/status.md after task N` is checked once, right after the *first* worker call, before sensors/code-health/review-corrective retries run; a later corrective call in the same task's cycle can still close the gap, making the warning stale by the time the task actually finishes. Confirmed 2026-08-12. Minor, not yet fixed. | known gap |
| Error recovery templates for recurring failure modes | pending |
| Per-task git commit in worktree branch | ✅ |
| Interactive merge prompt after successful run (y/n fast-forward into main) | ✅ |
| Merge prompt opens the branch's diff in a floating Zellij pane (`$EDITOR`) before asking y/n — personal workflow convenience, no-op outside Zellij | ✅ |
| PR creation for review | pending |
| **Keep the branch on task failure instead of `git branch -D`** — `handle.keep()` is called (with a `[loop]` message naming the branch and completed-task count) on sensor-retry exhaustion, sensor regression during review, **and now hard worker-execution failure too** (2026-08-12 — closes the gap this line used to call out as unfixed; triggered for real twice — an agent-work credits-exhaustion incident and a task-cli spend-limit incident — before it was fixed) | ✅ |
| `.agent-last-run.json` — durable record of the preserved branch's name/tip-commit/timestamp at every branch-preservation point, so recovery doesn't depend on terminal scrollback (2026-08-12) | ✅ |
| **Leak check on every task exit path, not just success** — `_run_one_task`'s body wrapped in `try/finally`, `_warn_if_leaked` moved into the `finally` so a leaked write is reported on worker failure and sensor-exhaustion too, not only the happy path (2026-08-12) | ✅ |
| `agent loop --resume` — detect an existing `agent/*` branch with completed task commits, skip them, continue from the first incomplete task | pending — separate follow-on, still blocked on other design questions (not this dependency) |
| `agent loop --plan <path>` (or auto-detected prompt) — approve/execute an existing `plan.md` without re-running the planner | pending |

---

## Phase 4 — Tracing & sub-agents

Goal: scale up autonomy — overnight runs, model routing. (Adversarial review moved to
Phase 2.3 — PRD, 2026-08-04.)

| Item | Status |
|---|---|
| Worker sandboxing (container/VM) — **required before FileGate and Ralph loop**; motivated further by the `cwd`-isolation leak found in Phase 1.1 (worktree sharing the main repo's `.git` isn't a hard boundary) | pending |
| Refactor sub-agent (`agent refactor <path>`) — read-only, pattern-drift/duplication findings, no `model:` field yet | ✅ |
| Architecture sub-agent (`agent architect [hint]`) — CLAIM/EXTRACT/DOUBT/RECONCILE bounded-cycle review, always whole-project scope with an optional free-text hint, `model: claude-opus-4-6`, AGENTS.md/roadmap/ADRs read as a required first step so findings are grounded in *why* the code is shaped that way, not just its current shape | ✅ |
| Native model routing — subagent `model:` frontmatter now actually takes effect (`ClaudeDriver` parses and passes `--model`); `planner.md`'s `claude-opus-4-6` and `reviewer.md`'s `claude-haiku-4-5` are the first real uses | ✅ |
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
