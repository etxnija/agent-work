# ADR-0005: Editable install as distribution model

## Status

Accepted

## Context

The harness needs to be available as `agent bootstrap` and `agent loop` from any project directory. The harness is a personal, actively evolving tool — not a stable versioned library. Options considered: copying files into each project, versioned releases on PyPI, editable install from source.

## Decision

Distribute via `pip install -e /path/to/agent-work` (editable install). The source stays in a single location. Each bootstrapped project gets a `mise.toml` that pins the same Python version, making `agent` available without a `mise exec` prefix.

## Consequences

- Editing any harness file takes effect immediately in all projects — no reinstall, no version bump.
- There is one canonical copy of the harness; projects do not drift from each other.
- This model is single-engineer only. Multi-engineer use would require a shared install path or a versioned release mechanism — both deferred as an open question.
- If the harness path moves, all projects need `pip install -e` re-run. Acceptable for personal tooling.
