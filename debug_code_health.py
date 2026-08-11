"""
debug_code_health.py — run check_code_health() against a real path, with the
raw lizard CSV and the two parsed-findings categories broken out separately,
so a reported "N findings" count can be traced back to where it actually
comes from instead of guessed at from a manual lizard run.

Usage (from the agent-work repo root, so `runner` is importable):
    python3 debug_code_health.py /path/to/worktree-or-checkout
"""
import subprocess
import sys
from pathlib import Path

from runner.code_health import (
    CCN_THRESHOLD,
    NLOC_THRESHOLD,
    _changed_files,
    _parse_duplicate_findings,
    _parse_metric_findings,
    check_code_health,
)


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 debug_code_health.py <path>")
        sys.exit(1)

    worktree = Path(sys.argv[1]).resolve()
    print(f"Target: {worktree}")
    print(f"Thresholds: NLOC > {NLOC_THRESHOLD}, CCN > {CCN_THRESHOLD}\n")

    changed = _changed_files(worktree)
    print(f"=== _changed_files() -> {len(changed)} file(s) ===")
    for f in changed:
        print(f"  {f}")
    print()

    if not changed:
        print("No changed files vs main — check_code_health() would return [] here.")
        return

    result = subprocess.run(
        ["lizard", "--csv", "-Eduplicate", *changed],
        cwd=worktree, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"lizard exited {result.returncode}, stderr:\n{result.stderr}")

    csv_text, _, duplicate_text = result.stdout.partition("Duplicates\n")

    metric_findings = _parse_metric_findings(csv_text)
    duplicate_findings = _parse_duplicate_findings(duplicate_text)

    print(f"=== Metric findings (length/complexity): {len(metric_findings)} ===")
    for f in metric_findings:
        print(f"  {f}")
    print()

    print(f"=== Duplicate findings: {len(duplicate_findings)} ===")
    for f in duplicate_findings:
        print(f"  {f}")
    print()

    total = check_code_health(worktree)
    print(f"=== check_code_health() total: {len(total)} ===")
    if len(total) != len(metric_findings) + len(duplicate_findings):
        print(
            "  ^ mismatch vs the sum of the two categories above — worth looking "
            "at check_code_health()'s own combination logic if this fires."
        )


if __name__ == "__main__":
    main()
