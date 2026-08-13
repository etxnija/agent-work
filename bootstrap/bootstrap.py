"""
bootstrap.py — set up a new project with the agent harness structure.

Usage (via CLI after pip install -e):
    agent bootstrap <project-dir> [--lang go|typescript|python]

Creates:
    <project-dir>/
        AGENTS.md               harness conventions for this project
        memory/
            status.md           session work log
        agents/                 sub-agent definitions (populated from harness)
        sensors/                lint/test/lsp scripts (language-specific stubs)
        .claude/
            agents/             sub-agent definitions (for Claude Code interactive sessions)
            settings.json       permissions allow/deny list + PreToolUse hook registration
            hooks/
                block-destructive.sh    blocks rm -rf, force-push, sudo, etc.
"""

import json
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
- Sensors (sensors/test.sh, sensors/lint.sh) run automatically after every task; you
  do not need a task for this
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

COVERAGERC_TEMPLATE = """\
[run]
source = .
omit =
    */test_*.py
    */conftest.py
    */.venv/*
    */venv/*
    */__pycache__/*
    */.git/*
    */logs/*
    */*.egg-info/*
"""

# Base allow list (all projects). Language-specific commands added below.
_SETTINGS_ALLOW_BASE = [
    "Bash(mise run*)",
    "Bash(git add*)",
    "Bash(git commit*)",
    "Bash(git status*)",
    "Bash(git diff*)",
    "Bash(git log*)",
    "Bash(git branch*)",
]

_SETTINGS_ALLOW_LANG = {
    "go":         ["Bash(go test*)", "Bash(go build*)", "Bash(golangci-lint*)"],
    "typescript": ["Bash(npm test*)", "Bash(npm run*)", "Bash(npx eslint*)"],
    "python":     ["Bash(pytest*)", "Bash(python -m pytest*)", "Bash(ruff*)", "Bash(pyright*)", "Bash(diff-cover*)"],
    "":           [],
}

_SETTINGS_DENY = [
    "Bash(rm -rf*)",
    "Bash(git push*)",
    "Bash(curl*)",
    "Bash(wget*)",
    "Bash(sudo*)",
    "Bash(pip install*)",
    "Read(./.env)",
    "Read(./.env.*)",
    "Read(~/.ssh/**)",
    "Read(~/.aws/**)",
    "Read(~/.claude/**)",
]

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
        "lint.sh": "#!/bin/sh\nset -e\nruff check --fix .\n",
        "test.sh": "#!/bin/sh\nset -e\npytest --cov=. --cov-report=xml --cov-report=json --cov-report=term-missing\ndiff-cover coverage.xml --compare-branch=main --fail-under=100\npython3 sensors/_coverage_floor.py\n",
        "lsp.sh": "#!/bin/sh\nset -e\npyright --outputjson\n",
        "_coverage_floor.py": "#!/usr/bin/env python3\n\"\"\"Check 2: whole-repo coverage regression floor vs. main's cached baseline.\"\"\"\n\nimport json\nimport sys\nfrom pathlib import Path\n\nTOLERANCE = 1.0\n\n\ndef main() -> int:\n    current = json.loads(Path(\"coverage.json\").read_text())[\"totals\"][\"percent_covered\"]\n\n    baseline_path = Path(\".coverage-baseline\")\n    if not baseline_path.exists():\n        print(\"[coverage] no baseline yet, skipping\")\n        return 0\n\n    baseline = float(baseline_path.read_text().strip())\n    drop = baseline - current\n\n    if drop > TOLERANCE:\n        print(\n            f\"Whole-repo coverage dropped from {baseline:.2f}% to {current:.2f}% \"\n            f\"(more than {TOLERANCE} point tolerance vs main).\"\n        )\n        return 1\n\n    print(f\"Whole-repo coverage: {current:.2f}% (baseline {baseline:.2f}%)\")\n    return 0\n\n\nif __name__ == \"__main__\":\n    sys.exit(main())\n",
    },
    "": {
        "lint.sh": "#!/bin/sh\n# TODO: add lint command for your stack\necho 'lint: not configured'\n",
        "test.sh": "#!/bin/sh\n# TODO: add test command for your stack\necho 'test: not configured'\n",
        "lsp.sh": "#!/bin/sh\n# TODO: add LSP/type-check command for your stack\necho 'lsp: not configured'\n",
    },
}


def _settings_json(lang: str) -> str:
    """Generate .claude/settings.json content for the given language."""
    allow = _SETTINGS_ALLOW_BASE + _SETTINGS_ALLOW_LANG.get(lang, [])
    return json.dumps(
        {
            "permissions": {
                "allow": allow,
                "deny": _SETTINGS_DENY,
            },
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/block-destructive.sh",
                            }
                        ],
                    }
                ]
            },
        },
        indent=2,
    ) + "\n"


def _write_if_missing(path: Path, content: str, project_dir: Path) -> None:
    if not path.exists():
        path.write_text(content)
        print(f"  created {path.relative_to(project_dir)}")


def _write_executable_if_missing(path: Path, content: str, project_dir: Path) -> None:
    if not path.exists():
        path.write_text(content)
        path.chmod(0o755)
        print(f"  created {path.relative_to(project_dir)}")


def _copy_if_missing(src: Path, dest: Path, project_dir: Path, executable: bool = False) -> None:
    if not dest.exists() and src.exists():
        shutil.copy(src, dest)
        if executable:
            dest.chmod(0o755)
        print(f"  created {dest.relative_to(project_dir)}")


def _make_dirs(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "memory").mkdir(exist_ok=True)
    (project_dir / "agents").mkdir(exist_ok=True)
    (project_dir / "sensors").mkdir(exist_ok=True)
    (project_dir / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    (project_dir / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)


def _copy_subagents(project_dir: Path) -> None:
    for src in sorted((HARNESS_ROOT / "agents").glob("*.md")):
        for dest_dir in [project_dir / "agents", project_dir / ".claude" / "agents"]:
            _copy_if_missing(src, dest_dir / src.name, project_dir)


def _write_sensors(project_dir: Path, lang: str) -> None:
    for name, content in SENSORS.get(lang, SENSORS[""]).items():
        _write_executable_if_missing(project_dir / "sensors" / name, content, project_dir)


def bootstrap(project_dir: Path, lang: str) -> None:
    from datetime import UTC, datetime

    if project_dir.exists() and any(project_dir.iterdir()):
        print(f"Warning: {project_dir} is not empty. Skipping existing files.")

    _make_dirs(project_dir)

    _write_if_missing(
        project_dir / "AGENTS.md",
        AGENTS_MD_TEMPLATE.format(lang=lang or "not specified", lang_testing=LANG_TESTING.get(lang, "")),
        project_dir,
    )
    _write_if_missing(
        project_dir / "memory" / "status.md",
        STATUS_MD_TEMPLATE.format(date=datetime.now(tz=UTC).date().isoformat(), lang=lang or "not specified"),
        project_dir,
    )
    _copy_subagents(project_dir)
    _write_if_missing(project_dir / "CLAUDE.md", CLAUDE_MD_TEMPLATE, project_dir)
    _write_if_missing(
        project_dir / "mise.toml",
        MISE_TOML_TEMPLATE.format(python_version=_harness_python_version()),
        project_dir,
    )

    if lang == "python":
        _write_if_missing(project_dir / ".coveragerc", COVERAGERC_TEMPLATE, project_dir)

    _write_sensors(project_dir, lang)

    _write_if_missing(project_dir / ".claude" / "settings.json", _settings_json(lang), project_dir)

    _copy_if_missing(
        HARNESS_ROOT / ".claude" / "hooks" / "block-destructive.sh",
        project_dir / ".claude" / "hooks" / "block-destructive.sh",
        project_dir,
        executable=True,
    )

    print(f"\nDone. Project ready at {project_dir}")
    print("Next: edit AGENTS.md with project-specific conventions, then run the planner.")


# Entry point is cli.py:main — run via `agent bootstrap <dir> [--lang ...]`
