"""Tests for runner/sandbox/."""

import os
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from runner.sandbox import get_sandbox
from runner.sandbox.base import WorkspaceHandle
from runner.sandbox.noop import NoopSandbox
from runner.sandbox.worktree import GitWorktreeSandbox

# ── WorkspaceHandle ───────────────────────────────────────────────────────────

class TestWorkspaceHandle:
    def test_keep_sets_flag(self, tmp_path):
        handle = WorkspaceHandle(path=tmp_path, branch="test")
        assert not handle._keep
        handle.keep()
        assert handle._keep

    def test_keep_is_false_by_default(self, tmp_path):
        handle = WorkspaceHandle(path=tmp_path, branch="")
        assert not handle._keep


# ── NoopSandbox ───────────────────────────────────────────────────────────────

class TestNoopSandbox:
    def test_yields_project_root(self, tmp_path):
        with NoopSandbox().workspace(tmp_path) as handle:
            assert handle.path == tmp_path

    def test_branch_is_empty_string(self, tmp_path):
        with NoopSandbox().workspace(tmp_path) as handle:
            assert handle.branch == ""

    def test_keep_does_not_raise(self, tmp_path):
        with NoopSandbox().workspace(tmp_path) as handle:
            handle.keep()  # should not raise

    def test_project_root_exists_after_exit(self, tmp_path):
        with NoopSandbox().workspace(tmp_path):
            pass
        assert tmp_path.exists()  # noop never deletes anything


# ── GitWorktreeSandbox ────────────────────────────────────────────────────────

def _init_repo(path: Path) -> None:
    """Create a git repo with one empty commit (required for git worktree)."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )


def _list_branches(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "branch"], cwd=repo, check=True, capture_output=True, text=True
    )
    return [b.strip().lstrip("* ") for b in result.stdout.splitlines() if b.strip()]


class TestGitWorktreeSandbox:
    def test_yields_path_distinct_from_project_root(self, tmp_path):
        _init_repo(tmp_path)
        with GitWorktreeSandbox().workspace(tmp_path) as handle:
            assert handle.path != tmp_path
            assert handle.path.exists()

    def test_branch_name_starts_with_agent(self, tmp_path):
        _init_repo(tmp_path)
        with GitWorktreeSandbox().workspace(tmp_path) as handle:
            assert handle.branch.startswith("agent/")

    def test_worktree_removed_on_exit(self, tmp_path):
        _init_repo(tmp_path)
        with GitWorktreeSandbox().workspace(tmp_path) as handle:
            worktree_path = handle.path
        assert not worktree_path.exists()

    def test_branch_deleted_when_not_kept(self, tmp_path):
        _init_repo(tmp_path)
        with GitWorktreeSandbox().workspace(tmp_path) as handle:
            branch = handle.branch
        assert branch not in _list_branches(tmp_path)

    def test_branch_kept_when_keep_called(self, tmp_path):
        _init_repo(tmp_path)
        with GitWorktreeSandbox().workspace(tmp_path) as handle:
            branch = handle.branch
            handle.keep()
        assert branch in _list_branches(tmp_path)
        # cleanup
        subprocess.run(["git", "branch", "-D", branch], cwd=tmp_path, check=True, capture_output=True)

    def test_worktree_removed_even_when_kept(self, tmp_path):
        _init_repo(tmp_path)
        with GitWorktreeSandbox().workspace(tmp_path) as handle:
            worktree_path = handle.path
            handle.keep()
        assert not worktree_path.exists()
        # cleanup
        subprocess.run(
            ["git", "branch", "-D", handle.branch], cwd=tmp_path, check=True, capture_output=True
        )


# ── get_sandbox factory ───────────────────────────────────────────────────────

class TestGetSandbox:
    CASES: ClassVar[list] = [
        pytest.param("worktree", GitWorktreeSandbox, id="worktree"),
        pytest.param("noop", NoopSandbox, id="noop"),
    ]

    @pytest.mark.parametrize("mode,expected_type", CASES)
    def test_factory(self, mode, expected_type, monkeypatch):
        monkeypatch.setenv("AGENT_SANDBOX", mode)
        assert isinstance(get_sandbox(), expected_type)

    def test_default_is_worktree(self, monkeypatch):
        monkeypatch.delenv("AGENT_SANDBOX", raising=False)
        assert isinstance(get_sandbox(), GitWorktreeSandbox)

    def test_unknown_mode_raises(self, monkeypatch):
        monkeypatch.setenv("AGENT_SANDBOX", "docker")
        with pytest.raises(ValueError, match="docker"):
            get_sandbox()
