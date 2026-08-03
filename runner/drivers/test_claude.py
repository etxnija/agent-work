"""Tests for ClaudeDriver and its helpers."""

from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from runner.drivers.claude import (
    ClaudeDriver,
    _inject_context,
    _load_agent_body,
    _load_agent_definition,
    _parse_frontmatter,
)

# ── _parse_frontmatter ────────────────────────────────────────────────────────

class TestParseFrontmatter:
    CASES: ClassVar[list] = [
        pytest.param(
            "---\nname: test\ntools: Read, Glob\n---\nBody here",
            {"name": "test", "tools": "Read, Glob"},
            "Body here",
            id="parses_name_and_tools",
        ),
        pytest.param(
            "No frontmatter",
            {},
            "No frontmatter",
            id="no_frontmatter_returns_empty_dict",
        ),
        pytest.param(
            "---\nname: test\n---\n\n  Stripped body  \n",
            {"name": "test"},
            "Stripped body",
            id="body_is_stripped",
        ),
    ]

    @pytest.mark.parametrize("content,expected_fields,expected_body", CASES)
    def test_parse(self, content, expected_fields, expected_body):
        fields, body = _parse_frontmatter(content)
        assert fields == expected_fields
        assert body == expected_body


# ── _load_agent_body ──────────────────────────────────────────────────────────

class TestLoadAgentBody:
    CASES: ClassVar[list] = [
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


# ── _load_agent_definition ────────────────────────────────────────────────────

class TestLoadAgentDefinition:
    def test_returns_body_and_tools(self, tmp_path, monkeypatch):
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "planner.md").write_text(
            "---\nname: planner\ntools: Read, Glob, Grep\n---\nYou plan."
        )

        import runner.drivers.claude as mod
        monkeypatch.setattr(mod, "_AGENT_SEARCH_PATHS", [agent_dir])

        body, tools = _load_agent_definition("planner")
        assert body == "You plan."
        assert tools == ["Read", "Glob", "Grep"]

    def test_no_tools_field_returns_empty_list(self, tmp_path, monkeypatch):
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "worker.md").write_text("---\nname: worker\n---\nYou work.")

        import runner.drivers.claude as mod
        monkeypatch.setattr(mod, "_AGENT_SEARCH_PATHS", [agent_dir])

        body, tools = _load_agent_definition("worker")
        assert body == "You work."
        assert tools == []

    def test_not_found_returns_none_and_empty(self, tmp_path, monkeypatch):
        import runner.drivers.claude as mod
        monkeypatch.setattr(mod, "_AGENT_SEARCH_PATHS", [tmp_path / "agents"])

        body, tools = _load_agent_definition("missing")
        assert body is None
        assert tools == []


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
    CASES: ClassVar[list] = [
        pytest.param(
            "hello",
            [],
            None,
            ["claude", "--print", "--dangerously-skip-permissions", "hello"],
            "response text",
            0,
            id="simple_prompt_no_context",
        ),
        pytest.param(
            "do something",
            [],
            None,
            ["claude", "--print", "--dangerously-skip-permissions", "do something"],
            "",
            1,
            id="non_zero_exit_code_propagated",
        ),
    ]

    @pytest.mark.parametrize("prompt,context_files,cwd,expected_args,stdout,returncode", CASES)
    def test_run(self, prompt, context_files, cwd, expected_args, stdout, returncode):
        mock_result = MagicMock()
        mock_result.stdout = stdout + "\n"
        mock_result.returncode = returncode

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = ClaudeDriver().run(prompt, context_files, cwd=cwd)

        mock_run.assert_called_once_with(
            expected_args, capture_output=True, text=True, cwd=cwd, check=False
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

    def test_run_passes_cwd_to_subprocess(self, tmp_path):
        mock_result = MagicMock(stdout="ok\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            ClaudeDriver().run("prompt", cwd=tmp_path)

        assert mock_run.call_args[1]["cwd"] == tmp_path


# ── ClaudeDriver.run_subagent ─────────────────────────────────────────────────

class TestClaudeDriverRunSubagent:
    def test_uses_allowed_tools_when_tools_declared(self, tmp_path, monkeypatch):
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "planner.md").write_text(
            "---\nname: planner\ntools: Read, Glob, Grep, Write\n---\nYou are the planner."
        )

        import runner.drivers.claude as mod
        monkeypatch.setattr(mod, "_AGENT_SEARCH_PATHS", [agent_dir])

        mock_result = MagicMock(stdout="plan written\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = ClaudeDriver().run_subagent("planner", "explore")

        cmd = mock_run.call_args[0][0]
        assert "--allowedTools" in cmd
        assert "Read,Glob,Grep,Write" in cmd
        # Both flags required: --allowedTools restricts tool set, --dangerously-skip-permissions
        # suppresses prompts so unattended Write calls don't hang the loop.
        assert "--dangerously-skip-permissions" in cmd
        assert "You are the planner." in cmd[-1]
        assert "explore" in cmd[-1]
        assert result.text == "plan written"

    def test_uses_dangerously_skip_when_no_tools_declared(self, tmp_path, monkeypatch):
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "worker.md").write_text("---\nname: worker\n---\nYou work.")

        import runner.drivers.claude as mod
        monkeypatch.setattr(mod, "_AGENT_SEARCH_PATHS", [agent_dir])

        mock_result = MagicMock(stdout="done\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            ClaudeDriver().run_subagent("worker", "task")

        cmd = mock_run.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd
        assert "--allowedTools" not in cmd

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
