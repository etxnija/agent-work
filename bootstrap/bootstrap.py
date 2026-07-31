"""
bootstrap.py — set up a new project with the agent harness structure.

Usage (via CLI after pip install -e):
    agent bootstrap <project-dir> [--lang go|typescript|python]

Creates:
    <project-dir>/
        AGENTS.md           harness conventions for this project
        memory/
            status.md       session work log
        agents/             sub-agent definitions (populated from harness)
        sensors/            lint/test/lsp scripts (language-specific stubs)
        .claude/
            agents/         symlink or copy of agents/ for Claude Code
"""

import re
import shutil
from pathlib import Path

HARNESS_ROOT = Path(__file__).parent.parent


def _harness_python_version() -> str:
    """Read the Python version from the harness mise.toml so it stays in sync."""
    mise_toml = HARNESS_ROOT / "mise.toml"
    if mise_toml.exists():
        match = re.search(r'^python\s*=\s*"([^"]+)"', mise_toml.read_text(), re.MULTILINE)
        if match:
            return match.group(1)
    return "3.12"  # fallback

AGENTS_MD_TEMPLATE = """\
# AGENTS.md

Project conventions for AI agents working in this codebase.
Keep this file up to date — it is the agent's long-term memory.

## Stack
Language: {lang}

## Patterns & Conventions
- (add project-specific patterns here)

## Gotchas
- (add things that have tripped agents up)

## Testing
- Run all tests before committing
- New code must have tests; do not delete tests to make coverage pass
{lang_testing}

## Style
- Follow the existing code style in the repo
- Prefer simple, readable code over clever code
"""

LANG_TESTING = {
    "go":         "- Use table-driven tests (see existing *_test.go files for examples)",
    "typescript": "- Use the test framework already in the project (check package.json)",
    "python":     "- Use pytest; follow fixture patterns in existing tests",
    "":           "",
}

STATUS_MD_TEMPLATE = """\
# Work Log

Session-to-session progress log. Distinct from AGENTS.md (stable rules).

---

## {date} — project bootstrapped

### Done
- Bootstrapped with agent harness (lang: {lang})

### Next
- (add first task here)
"""

CLAUDE_MD_TEMPLATE = """\
# CLAUDE.md

At the start of every session, read these two files before doing anything else:

- **AGENTS.md** — project conventions, stack, testing rules, and gotchas
- **memory/status.md** — session-to-session work log; what has been done and what is next

## Agent harness

This project is managed with the agent harness. Tasks are run via:

```bash
agent loop "task description"
```

The planner sub-agent explores the codebase and writes a plan for human approval before any code is written. Plans are stored in `plan.md`.
"""

MISE_TOML_TEMPLATE = """\
[tools]
# Same Python version as the agent harness — makes `agent` available directly
# in this directory without needing `mise exec`.
python = "{python_version}"
"""

SENSORS = {
    "go": {
        "lint.sh": "#!/bin/sh\nset -e\ngolangci-lint run ./...\n",
        "test.sh": "#!/bin/sh\nset -e\ngo test ./... -race -count=1\n",
    },
    "typescript": {
        "lint.sh": "#!/bin/sh\nset -e\nnpx eslint .\n",
        "test.sh": "#!/bin/sh\nset -e\nnpm test\n",
    },
    "python": {
        "lint.sh": "#!/bin/sh\nset -e\nruff check .\n",
        "test.sh": "#!/bin/sh\nset -e\npytest\n",
    },
    "": {
        "lint.sh": "#!/bin/sh\n# TODO: add lint command for your stack\necho 'lint: not configured'\n",
        "test.sh": "#!/bin/sh\n# TODO: add test command for your stack\necho 'test: not configured'\n",
    },
}


def bootstrap(project_dir: Path, lang: str) -> None:
    if project_dir.exists() and any(project_dir.iterdir()):
        print(f"Warning: {project_dir} is not empty. Skipping existing files.")

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "memory").mkdir(exist_ok=True)
    (project_dir / "agents").mkdir(exist_ok=True)
    (project_dir / "sensors").mkdir(exist_ok=True)
    (project_dir / ".claude" / "agents").mkdir(parents=True, exist_ok=True)

    # AGENTS.md
    agents_md = project_dir / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(
            AGENTS_MD_TEMPLATE.format(
                lang=lang or "not specified",
                lang_testing=LANG_TESTING.get(lang, ""),
            )
        )
        print(f"  created {agents_md.relative_to(project_dir)}")

    # memory/status.md
    from datetime import date
    status_md = project_dir / "memory" / "status.md"
    if not status_md.exists():
        status_md.write_text(
            STATUS_MD_TEMPLATE.format(
                date=date.today().isoformat(),
                lang=lang or "not specified",
            )
        )
        print(f"  created {status_md.relative_to(project_dir)}")

    # Copy planner sub-agent
    src_planner = HARNESS_ROOT / "agents" / "planner.md"
    for dest_dir in [project_dir / "agents", project_dir / ".claude" / "agents"]:
        dest = dest_dir / "planner.md"
        if not dest.exists() and src_planner.exists():
            shutil.copy(src_planner, dest)
            print(f"  created {dest.relative_to(project_dir)}")

    # CLAUDE.md — bridges interactive Claude Code sessions to AGENTS.md and status.md
    claude_md = project_dir / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(CLAUDE_MD_TEMPLATE)
        print(f"  created {claude_md.relative_to(project_dir)}")

    # mise.toml — makes `agent` available without `mise exec` prefix
    mise_toml = project_dir / "mise.toml"
    if not mise_toml.exists():
        mise_toml.write_text(
            MISE_TOML_TEMPLATE.format(python_version=_harness_python_version())
        )
        print(f"  created {mise_toml.relative_to(project_dir)}")

    # Sensor scripts
    scripts = SENSORS.get(lang, SENSORS[""])
    for name, content in scripts.items():
        sensor = project_dir / "sensors" / name
        if not sensor.exists():
            sensor.write_text(content)
            sensor.chmod(0o755)
            print(f"  created {sensor.relative_to(project_dir)}")

    print(f"\nDone. Project ready at {project_dir}")
    print("Next: edit AGENTS.md with project-specific conventions, then run the planner.")


# Entry point is cli.py:main — run via `agent bootstrap <dir> [--lang ...]`
