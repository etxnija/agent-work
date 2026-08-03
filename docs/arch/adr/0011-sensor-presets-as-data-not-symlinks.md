# ADR-0011: Sensor defaults as copied preset files, not symlinks or per-language code

## Status

Accepted

## Context

Phase 2 (sensors) introduces `sensors/lsp.sh` alongside the existing `lint.sh` /
`test.sh`. All three are populated at bootstrap time from `bootstrap/bootstrap.py`'s
`SENSORS` dict — a Python dict literal keyed by `"go" | "typescript" | "python" | ""`,
each value a hardcoded multi-line shell string.

This doesn't scale past the current two-and-a-bit languages:

- **Every new language, or even a variant within a language** (e.g. `mypy` instead of
  `pyright`, `tox` instead of bare `pytest`), requires editing `bootstrap.py`'s Python
  source and shipping a new harness release. The opinion (what command represents
  "clean" for a stack) is welded to the mechanism (a Python dict inside the harness
  CLI).
- **Opinions drift.** If the harness later decides `pyright --outputjson` should run in
  strict mode, or `ruff check .` should gain a flag, every already-bootstrapped project
  is stuck with whatever was copied in at bootstrap time. There is no propagation path
  short of manually editing each project's `sensors/*.sh`.
- The end goal is code with zero outstanding issues per project, moving toward 100%
  coverage and broader code-health sensors (see roadmap Phase 2 "Metrics" /
  coverage row). Every new sensor category multiplies this problem if it stays
  hardcoded per-language Python.

**Symlinks, considered and rejected.** An earlier idea was to symlink a project's
`sensors/lint.sh` etc. directly to canonical scripts inside the `agent-work` checkout,
so updating the harness's copy updates every project instantly. Rejected: this breaks
the existing design rule that sensors "must run without the harness, from any terminal
or CI" (`AGENTS.md`) — a symlink to an absolute path like `~/source/agent-work/...`
is dangling on any other machine, teammate checkout, or CI runner. Live linkage and
"must survive without the harness present" are structurally in tension.

## Decision

Separate the **mechanism** (already generic — `_run_sensors()` from the Phase 2 plan
globs `sensors/*.sh` and doesn't care what's inside them; see ADR-0010) from the
**content** (the opinionated command for a given stack), and move the content from
Python code to data:

1. **Presets live as real files in `agent-work`**, not a Python dict:
   `presets/<lang>/{lint.sh,test.sh,lsp.sh}`. Adding a language or a variant is "add a
   directory of files," not "edit `bootstrap.py`'s logic."
2. **Bootstrap copies preset files into the new project**, unchanged from today's
   behavior of writing real, standalone files into `sensors/`. This preserves the
   zero-dependency, runs-anywhere property — nothing about running a bootstrapped
   project's sensors depends on `agent-work` being installed or reachable.
3. **Bootstrap-time override**: the prompt for lint/test/lsp commands shows the
   preset as a pre-filled default (accept with Enter); typing a replacement covers
   within-language variance (`tox` vs `pytest`, `mypy` vs `pyright`) without needing a
   preset for every combination.
4. **Propagation is an explicit, reviewable sync, not automatic.** A future
   `agent sensors sync` command diffs a project's `sensors/` against the current
   presets and lets the user apply or skip changes per file — the same shape as
   `chezmoi apply` or a Homebrew formula update: central opinion, explicit pull, never
   silently overwrites a hand-edited file.
5. **Future code-health sensors (coverage, etc.) are just new preset files.** They
   reuse `_run_sensors()` and the sync command unchanged — no `loop.py` changes
   required per new sensor category.

This ADR does not change the already-approved Phase 2 plan (`plan.md`, Python-only
`pyright` sensor) — that still lands using the existing `SENSORS` dict. The
preset-as-data migration and `agent sensors sync` are tracked separately as Phase 2.1
(see roadmap).

## Consequences

- Adding a language or stack variant no longer requires a harness source change —
  it's a new directory under `presets/`.
- Existing bootstrapped projects can pull updated opinions on demand via
  `agent sensors sync`, without the harness silently rewriting their files.
- `bootstrap.py`'s `SENSORS` dict and `_SETTINGS_ALLOW_LANG` become migration debt:
  Phase 2's Python-only work still uses them as-is; Phase 2.1 must migrate both to the
  preset-file model rather than extending the dict further.
- `agent sensors sync` is new surface area (diffing, per-file apply, conflict with
  local overrides) that doesn't exist yet — deferred, not designed in detail here.
- Bootstrap-time command prompts add a step to `agent bootstrap`; needs a sensible
  non-interactive default (accept all presets) for scripted/CI bootstrap invocations.
