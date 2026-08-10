#!/usr/bin/env python3
"""
agent — harness CLI

    agent bootstrap <project-dir> [--lang go|typescript|python]
    agent loop [task]

Install once (editable — source stays in place, no reinstall on edits):
    pip install -e /path/to/agent-work

Then run from any bootstrapped project directory:
    cd ~/projects/my-app
    agent bootstrap . --lang go
    agent loop "add a /health endpoint"
"""

import argparse
import sys
from pathlib import Path


def cmd_bootstrap(args) -> int:
    from bootstrap.bootstrap import bootstrap
    bootstrap(Path(args.project_dir), args.lang)
    return 0


def cmd_loop(args) -> int:
    from runner.loop import run_loop
    task = args.task or input("Task: ").strip()
    if not task:
        print("[error] No task provided.")
        return 1
    return run_loop(task)


def cmd_refactor(args) -> int:
    from runner.drivers import get_driver

    target = Path(args.path)
    prompt = f"Review {target} for refactor drift per your instructions."
    result = get_driver().run_subagent("refactor", prompt, cwd=Path.cwd())
    if result.exit_code != 0:
        print(f"[error] {result.text}")
        return 1
    print(result.text)
    return 0


def cmd_architect(args) -> int:
    from runner.architecture import run_architecture_review
    return run_architecture_review(args.hint, Path.cwd())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Agent harness CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bp = sub.add_parser(
        "bootstrap",
        help="Set up a new project with the harness structure",
    )
    bp.add_argument("project_dir", help="Directory to bootstrap (created if absent)")
    bp.add_argument(
        "--lang",
        choices=["go", "typescript", "python"],
        default="",
        help="Primary language — affects sensor stubs and AGENTS.md (optional)",
    )

    lp = sub.add_parser(
        "loop",
        help="Run one plan → approve → implement cycle",
    )
    lp.add_argument("task", nargs="?", help="Task description (prompted if omitted)")

    rp = sub.add_parser(
        "refactor",
        help="Flag drift from established codebase patterns",
    )
    rp.add_argument("path", help="File or directory to review")

    ap = sub.add_parser(
        "architect",
        help="Review the project's architectural shape",
    )
    ap.add_argument(
        "hint",
        nargs="?",
        default=None,
        help="Optional pointer to what looks off, to help focus the review — reviews the whole project either way.",
    )

    args = parser.parse_args()

    match args.command:
        case "bootstrap":
            sys.exit(cmd_bootstrap(args))
        case "loop":
            sys.exit(cmd_loop(args))
        case "refactor":
            sys.exit(cmd_refactor(args))
        case "architect":
            sys.exit(cmd_architect(args))


if __name__ == "__main__":
    main()
