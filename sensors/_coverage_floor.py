#!/usr/bin/env python3
"""Check 2: whole-repo coverage regression floor vs. main's cached baseline."""

import json
import sys
from pathlib import Path

TOLERANCE = 1.0


def main() -> int:
    current = json.loads(Path("coverage.json").read_text())["totals"]["percent_covered"]

    baseline_path = Path(".coverage-baseline")
    if not baseline_path.exists():
        print("[coverage] no baseline yet, skipping")
        return 0

    baseline = float(baseline_path.read_text().strip())
    drop = baseline - current

    if drop > TOLERANCE:
        print(
            f"Whole-repo coverage dropped from {baseline:.2f}% to {current:.2f}% "
            f"(more than {TOLERANCE} point tolerance vs main)."
        )
        return 1

    print(f"Whole-repo coverage: {current:.2f}% (baseline {baseline:.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
