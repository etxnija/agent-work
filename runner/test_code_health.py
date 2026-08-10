import os
import subprocess
from pathlib import Path

from runner.code_health import _parse_metric_findings, check_code_health


def _init_repo(path: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"],
                   cwd=path, check=True, capture_output=True, env=env)


def _commit_on_feature_branch(path: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=path,
                    check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=path,
                    check=True, capture_output=True, env=env)


class TestParseMetricFindings:
    def test_blank_row_is_skipped(self):
        assert _parse_metric_findings("\n") == []


class TestCheckCodeHealth:
    def test_clean_file_returns_no_findings(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("def add(a, b):\n    return a + b\n")
        _commit_on_feature_branch(tmp_path)

        assert check_code_health(tmp_path) == []

    def test_nloc_violation_is_flagged(self, tmp_path):
        _init_repo(tmp_path)
        lines = ["def big_function(x):", "    result = 0"]
        for i in range(1, 56):
            lines.append(f"    result += x * {i}")
        lines.append("    return result")
        (tmp_path / "nloc.py").write_text("\n".join(lines) + "\n")
        _commit_on_feature_branch(tmp_path)

        findings = check_code_health(tmp_path)

        assert len(findings) == 1
        assert "big_function" in findings[0]
        assert "lines of code" in findings[0]

    def test_ccn_violation_is_flagged(self, tmp_path):
        _init_repo(tmp_path)
        lines = ["def branchy(x):", "    result = 0"]
        for i in range(1, 21):
            lines.append(f"    if x == {i}:")
            lines.append(f"        result = {i}")
        lines.append("    return result")
        (tmp_path / "ccn.py").write_text("\n".join(lines) + "\n")
        _commit_on_feature_branch(tmp_path)

        findings = check_code_health(tmp_path)

        assert len(findings) == 1
        assert "branchy" in findings[0]
        assert "complexity" in findings[0]

    def test_duplicate_blocks_are_flagged(self, tmp_path):
        _init_repo(tmp_path)
        body = ["def alpha(x):", "    total = 0"]
        for i in range(1, 25):
            body.append(f"    total += x * {i}")
            body.append(f"    total -= {i}")
        body.append("    return total")
        (tmp_path / "dup_a.py").write_text("\n".join(body) + "\n")

        body2 = ["def beta(x):", "    total = 0"]
        for i in range(1, 25):
            body2.append(f"    total += x * {i}")
            body2.append(f"    total -= {i}")
        body2.append("    return total")
        (tmp_path / "dup_b.py").write_text("\n".join(body2) + "\n")
        _commit_on_feature_branch(tmp_path)

        findings = check_code_health(tmp_path)

        assert any("uplicate" in f for f in findings)

    def test_no_changed_files_returns_empty(self, tmp_path):
        _init_repo(tmp_path)

        assert check_code_health(tmp_path) == []
