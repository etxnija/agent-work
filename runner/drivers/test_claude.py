"""Tests for ClaudeDriver and its helpers."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from runner.drivers.claude import ClaudeDriver, _inject_context, _load_agent_body


# ── _load_agent_body ──────────────────────────────────────────────────────────

class TestLoadAgentBody:
    CASES = [
        pytest.param(
            "---\nname: test\ntools: Read\n---\nBody content here",
            "Body content here",
            id="strips_yaml_frontmatter",
        ),
        pytest.param(
            "No frontmatter, just body",
            "No frontmatter, just body",
            id="no_frontmatter_returned_as_is",
        ),
        pytest.param(
            "---\nname: test\n---\n\n  Leading whitespace  \n",
            "Leading whitespace",
            id="body_is_stripped",
        ),
    ]

    @pytest.mark.parametrize("content,expected", CASES)
    def test_load(self, tmp_path, monkeypatch, content, expected):
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "myagent.md").write_text(content)

        import runner.drivers.claude as mod
        monkeypatch.setattr(mod, "_AGENT_SEARCH_PATHS", [agent_dir])

        assert _load_agent_body("myagent") == expected

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        import runner.drivers.claude as mod
        monkeypatch.setattr(mod, "_AGENT_SEARCH_PATHS", [tmp_path / "agents"])

        assert _load_agent_body("nonexistent") is None


# ── _inject_context ───────────────────────────────────────────────────────────

class TestInjectContext:
    def test_no_files_returns_prompt_unchanged(self):
        assert _inject_context("my prompt", []) == "my prompt"

    def test_existing_file_prepended(self, tmp_path):
        f = tmp_path / "ctx.md"
        f.write_text("context content")
        result = _inject_context("prompt", [str(f)])
        assert f'<file path="{f}">' in result
        assert "context content" in result
        assert result.endswith("prompt")

    def test_missing_file_skipped(self, tmp_path):
        result = _inject_context("prompt", [str(tmp_path / "missing.md")])
        assert result == "prompt"

    def test_multiple_files_all_prepended(self, tmp_path):
        files = []
        for i in range(3):
            f = tmp_path / f"f{i}.md"
            f.write_text(f"content {i}")
            files.append(str(f))
        result = _inject_context("prompt", files)
        for i in range(3):
            assert f"content {i}" in result
        assert result.endswith("prompt")


# ── ClaudeDriver.run ──────────────────────────────────────────────────────────

class TestClaudeDriverRun:
    CASES = [
        pytest.param(
            "hello",
            [],
            ["claude", "--print", "--dangerously-skip-permissions", "hello"],
            "response text",
            0,
            id="simple_prompt_no_context",
        ),
        pytest.param(
            "do something",
            [],
            ["claude", "--print", "--dangerously-skip-permissions", "do something"],
            "",
            1,
            id="non_zero_exit_code_propagated",
        ),
    ]

    @pytest.mark.parametrize("prompt,context_files,expected_args,stdout,returncode", CASES)
    def test_run(self, prompt, context_files, expected_args, stdout, returncode):
        mock_result = MagicMock()
        mock_result.stdout = stdout + "\n"
        mock_result.returncode = returncode

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = ClaudeDriver().run(prompt, context_files)

        mock_run.assert_called_once_with(
            expected_args, capture_output=True, text=True
        )
        assert result.text == stdout
        assert result.exit_code == returncode

    def test_run_injects_context_files(self, tmp_path):
        ctx = tmp_path / "ctx.md"
        ctx.write_text("important context")

        mock_result = MagicMock(stdout="ok\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            ClaudeDriver().run("prompt", [str(ctx)])

        called_prompt = mock_run.call_args[0][0][-1]
        assert "important context" in called_prompt
        assert "prompt" in called_prompt


# ── ClaudeDriver.run_subagent ─────────────────────────────────────────────────

class TestClaudeDriverRunSubagent:
    def test_composes_agent_body_into_prompt(self, tmp_path, monkeypatch):
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "planner.md").write_text("---\nname: planner\n---\nYou are the planner.")

        import runner.drivers.claude as mod
        monkeypatch.setattr(mod, "_AGENT_SEARCH_PATHS", [agent_dir])

        mock_result = MagicMock(stdout="plan written\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = ClaudeDriver().run_subagent("planner", "explore the codebase")

        called_prompt = mock_run.call_args[0][0][-1]
        assert "You are the planner." in called_prompt
        assert "explore the codebase" in called_prompt
        assert result.text == "plan written"

    def test_returns_error_when_agent_not_found(self, tmp_path, monkeypatch):
        import runner.drivers.claude as mod
        monkeypatch.setattr(mod, "_AGENT_SEARCH_PATHS", [tmp_path / "agents"])

        result = ClaudeDriver().run_subagent("missing", "task")

        assert result.exit_code == 1
        assert "missing" in result.text
