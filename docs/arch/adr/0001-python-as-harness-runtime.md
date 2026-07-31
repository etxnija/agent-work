# ADR-0001: Python as harness runtime language

## Status

Accepted

## Context

The harness needs a language to implement the orchestration loop, driver abstraction, and bootstrap tooling. The goal is to support multiple AI tools (Claude, Gemini, and others) without rebuilding the loop. Every major model provider has a Python SDK. All major AI CLIs are callable from Python subprocesses. The harness has no complex UI or build requirements.

## Decision

Python 3.12+, managed via mise, with no third-party dependencies beyond the stdlib.

## Consequences

- Universal: every AI provider ships a Python SDK, so future drivers require no new runtime.
- No third-party deps means no lockfile churn and nothing to audit — the harness installs cleanly anywhere Python is present.
- Requires Python 3.12 on PATH; handled via `mise.toml` in both the harness repo and every bootstrapped project.
- TypeScript and Go were considered; both have good ecosystem support but are less universal across AI tooling and add build steps.
