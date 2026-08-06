"""Integration test for sensors/test.sh's check 1 (diff-aware 100% coverage via diff-cover).

Builds a throwaway git repo with a covered module on `main`, adds a new
function on top of it, then hand-writes a Cobertura coverage.xml for the
new lines in both an uncovered and a covered variant to prove diff-cover
actually fires on the former and passes the latter.
"""

import os
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@t.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@t.com",
}

_COVERAGE_XML_TEMPLATE = """<?xml version="1.0" ?>
<coverage line-rate="1.0" version="1.0">
  <packages>
    <package name="mypkg" line-rate="1.0">
      <classes>
        <class name="mod" filename="mypkg/mod.py" line-rate="1.0">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="1"/>
            <line number="4" hits="{new_lines_hits}"/>
            <line number="5" hits="{new_lines_hits}"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-m", "main"], cwd=path, check=True, capture_output=True)
    pkg = path / "mypkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("def covered():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, env=_ENV
    )


def _add_new_function(path: Path) -> None:
    mod = path / "mypkg" / "mod.py"
    mod.write_text(mod.read_text() + "\ndef new_func():\n    return 2\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add new_func"],
        cwd=path,
        check=True,
        capture_output=True,
        env=_ENV,
    )


def _run_diff_cover(path: Path, coverage_xml: str) -> subprocess.CompletedProcess:
    (path / "coverage.xml").write_text(coverage_xml)
    return subprocess.run(
        ["diff-cover", "coverage.xml", "--compare-branch=main", "--fail-under=100"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )


class TestDiffCoverCheck:
    CASES: ClassVar[list] = [
        pytest.param(0, True, ["Missing lines"], id="new_lines_uncovered_fails"),
        pytest.param(1, False, [], id="new_lines_covered_passes"),
    ]

    def _setup(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "checkout", "-b", "feature"], cwd=tmp_path, check=True, capture_output=True
        )
        _add_new_function(tmp_path)

    @pytest.mark.parametrize("new_lines_hits,expect_failure,expected_substrings", CASES)
    def test_diff_cover_check(self, tmp_path, new_lines_hits, expect_failure, expected_substrings):
        self._setup(tmp_path)
        result = _run_diff_cover(
            tmp_path, _COVERAGE_XML_TEMPLATE.format(new_lines_hits=new_lines_hits)
        )
        assert (result.returncode != 0) == expect_failure
        for substring in expected_substrings:
            assert substring in result.stdout

    def test_same_branch_empty_diff_passes(self, tmp_path):
        """`_update_coverage_baseline()` runs this check with HEAD already on
        `main`, so the diff against `--compare-branch=main` is always empty."""
        _init_repo(tmp_path)
        coverage_xml = _COVERAGE_XML_TEMPLATE.format(new_lines_hits=1)
        result = _run_diff_cover(tmp_path, coverage_xml)
        assert result.returncode == 0
        assert "No lines with coverage information in this diff." in result.stdout
