"""
code_health.py — lizard-based complexity and duplication check.

Self-contained: does not import from runner/loop.py.
"""

import csv
import io
import re
import subprocess
from pathlib import Path

CCN_THRESHOLD = 15
NLOC_THRESHOLD = 50

_DUPLICATE_MARKER = "Duplicates\n"
_BLOCK_START = "Duplicate block:"
_LOCATION_RE = re.compile(r"^(.+):(\d+) ~ (\d+)$")


def _changed_files(worktree: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "main"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [
        f
        for f in candidates
        if (worktree / f).exists()
        and not (Path(f).name.startswith("test_") and Path(f).name.endswith(".py"))
    ]


def _parse_metric_findings(csv_text: str) -> list[str]:
    findings = []
    for row in csv.reader(io.StringIO(csv_text)):
        if not row:
            continue
        nloc, ccn = int(row[0]), int(row[1])
        file_path, func_name = row[6], row[7]
        start_line, end_line = row[9], row[10]
        location = f"{file_path}: {func_name} (lines {start_line}-{end_line})"
        if nloc > NLOC_THRESHOLD:
            findings.append(
                f"{location} — lines of code {nloc} exceeds threshold {NLOC_THRESHOLD}"
            )
        if ccn > CCN_THRESHOLD:
            findings.append(
                f"{location} — cyclomatic complexity {ccn} exceeds threshold {CCN_THRESHOLD}"
            )
    return findings


def _parse_duplicate_findings(duplicate_text: str) -> list[str]:
    findings = []
    lines = duplicate_text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == _BLOCK_START:
            i += 1
            locations = []
            while i < len(lines) and not lines[i].startswith("^^"):
                match = _LOCATION_RE.match(lines[i].strip())
                if match:
                    file_path, start_line, end_line = match.groups()
                    locations.append(f"{file_path}:{start_line}-{end_line}")
                i += 1
            if locations:
                findings.append(f"Duplicate code: {', '.join(locations)}")
        else:
            i += 1
    return findings


def check_code_health(worktree: Path) -> list[str]:
    """
    Run lizard against files changed vs. main and flag functions exceeding
    NLOC_THRESHOLD or CCN_THRESHOLD, plus duplicate code blocks.

    Returns a list of human-readable finding strings, empty if clean or if
    no files changed.
    """
    changed = _changed_files(worktree)
    if not changed:
        return []

    result = subprocess.run(
        ["lizard", "--csv", "-Eduplicate", *changed],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )

    csv_text, _, duplicate_text = result.stdout.partition(_DUPLICATE_MARKER)

    findings = _parse_metric_findings(csv_text)
    findings.extend(_parse_duplicate_findings(duplicate_text))
    return findings
