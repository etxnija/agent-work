# ADR-0008: Per-task implementation replaces whole-plan implementation

## Status

Accepted

## Context

The original loop approved a plan and then sent the entire plan to the worker in a single call. Plan size, implementation size, and eventual PR/diff size were all the same unbounded thing. The first trial run on ev-decide produced a plan with a single large "Approach" narrative, and the worker implemented it all at once.

Three concrete problems follow from this:

1. **Sensor feedback is hard to localise.** A lint or test failure on a 300-line diff requires finding the offending line among many changes. A failure on a 30-line diff is immediately obvious. Phase 2 sensors are only actionable if diffs are small.

2. **PRs become unreviewable.** Phase 3 introduces git commits and PRs. A PR covering an entire feature in one commit cannot be meaningfully reviewed by a human or an adversarial-review agent (Phase 4).

3. **Failure recovery is coarse.** If the worker fails midway through a large single-call implementation, there is no clean recovery point. Per-task execution gives a natural stopping point: completed tasks are done, the failed task is isolated.

## Decision

Split the plan into independent, INVEST-sized tasks at the plan level, and implement them one at a time in the loop.

**Planner:** `## Approach` is replaced by `## Tasks` — a numbered list where each task specifies a title, the files it touches, and what to do. The planner is instructed to keep splitting until each task touches at most a bounded set of files and addresses one concern.

**Loop:** After plan approval, `_parse_tasks()` extracts the numbered task list. The worker is called once per task with only that task's text as its work item plus the full plan as context. A status.md entry and (Phase 2) sensor run happen after each task. A failure on task N stops the loop at N, leaving tasks 1…N-1 complete.

## Consequences

- Plan format change is a breaking change for any existing plan.md files — they will produce a "no tasks found" error until re-planned.
- The planner prompt is more prescriptive. If the planner doesn't follow the format, `_parse_tasks()` returns empty and the loop fails fast with a clear message.
- Per-task status.md entries give a fine-grained work log instead of one entry per loop run.
- Phase 2 sensor hooks and Phase 3 commit hooks are now per-task, which is where they belong.
- This is a prerequisite for Phase 2 sensor feedback to be locally actionable and for Phase 3/4 commits and PRs to be reviewably sized.
