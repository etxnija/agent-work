"""Tests for sensors/_coverage_floor.py (check 2: whole-repo regression floor)."""

import json
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

SCRIPT = Path(__file__).parent / "_coverage_floor.py"


def _write_fixtures(tmp_path: Path, current: float, baseline: float | None) -> None:
    (tmp_path / "coverage.json").write_text(
        json.dumps({"totals": {"percent_covered": current}})
    )
    if baseline is not None:
        (tmp_path / ".coverage-baseline").write_text(str(baseline))


def _run(tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True, check=False
    )


class TestCoverageFloor:
    CASES: ClassVar[list] = [
        pytest.param(80.0, 85.0, 1, ["85.00", "80.00"], id="drop_beyond_tolerance_fails"),
        pytest.param(84.5, 85.0, 0, [], id="drop_within_tolerance_passes"),
        pytest.param(90.0, 85.0, 0, [], id="coverage_improved_passes"),
        pytest.param(80.0, None, 0, ["no baseline yet, skipping"], id="no_baseline_skips"),
    ]

    @pytest.mark.parametrize("current,baseline,expected_returncode,expected_substrings", CASES)
    def test_coverage_floor(
        self, tmp_path, current, baseline, expected_returncode, expected_substrings
    ):
        _write_fixtures(tmp_path, current=current, baseline=baseline)
        result = _run(tmp_path)
        assert result.returncode == expected_returncode
        for substring in expected_substrings:
            assert substring in result.stdout
