---
type: decision
tags: [sandboxing, worktree, security, permissions]
summary: Git worktree isolation and .claude permission rules for worker tasks.
links: [docs/arch/adr/0007-sandboxing-deferred-to-phase-4.md, docs/arch/adr/0009-sandboxing-phase-1-1-worktree-and-tool-grants.md]
updated: 2026-08-11
---

# Sandboxing & Permissions

## Context & Rationale
Worker tasks must run in isolation so uncommitted edits, failed experiments, or destructive commands do not corrupt the main checkout or leak into untracked files. Full container/VM isolation was deferred per ADR-0007 until unattended `FileGate` operations ship.

## Key Rules & Mechanics
- **`GitWorktreeSandbox` (`runner/sandbox/worktree.py`):** Spawns a disposable git worktree and temporary branch for each worker run. On task success, the branch is kept for squash-merging; on task failure, `handle.keep()` preserves the branch for manual recovery.
- **Planner Permission Boundary:** The Planner sub-agent runs read-only using `--allowedTools Read,Glob,Grep` (no `Bash` or `Write`), enforced at the CLI tool level.
- **Worker Permission Boundary:** The Worker runs with `--dangerously-skip-permissions` but is constrained by `.claude/settings.json` allow/deny lists and the `PreToolUse` hook.
- **Destructive Command Hook (`.claude/hooks/block-destructive.sh`):** Intercepts `Bash` tool calls to block high-risk commands (`rm -rf`, `git push --force`, `sudo`, `dd`) regardless of permission flags.

## Gotchas & Failure Modes
- **`cwd`-Based Leakage:** `cwd`-based worktree isolation is not a hard OS process boundary. A worker can occasionally write outside its assigned worktree into the main checkout via absolute paths.
- **Main Checkout Detection:** `_main_checkout_dirty_paths()` snapshots `git status` on the main checkout before and after each task, emitting a `[warning]` if any non-`status.md` file is mutated outside the assigned worktree.
- **Shared `.git` Directory:** Worktrees share the primary repository's `.git` folder; status writes go to the project root via absolute path by design.
