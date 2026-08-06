"""Tests for cli.py's cmd_refactor."""

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cli
from cli import cmd_refactor
from runner.drivers.base import AgentResult


class TestCmdRefactor:
    def test_calls_driver_with_refactor_agent_and_target_path(self):
        mock_driver = MagicMock()
        mock_driver.run_subagent.return_value = AgentResult(text="findings", exit_code=0)
        with patch("runner.drivers.get_driver", return_value=mock_driver):
            cmd_refactor(argparse.Namespace(path="some/file.py"))

        mock_driver.run_subagent.assert_called_once()
        call_args = mock_driver.run_subagent.call_args
        assert call_args.args[0] == "refactor"
        assert "some/file.py" in call_args.args[1]
        assert call_args.kwargs["cwd"] == Path.cwd()

    def test_prints_result_text_on_success(self, capsys):
        mock_driver = MagicMock()
        mock_driver.run_subagent.return_value = AgentResult(text="findings", exit_code=0)
        with patch("runner.drivers.get_driver", return_value=mock_driver):
            result = cmd_refactor(argparse.Namespace(path="some/file.py"))

        assert result == 0
        assert "findings" in capsys.readouterr().out

    def test_nonzero_exit_prints_error_and_returns_one(self, capsys):
        mock_driver = MagicMock()
        mock_driver.run_subagent.return_value = AgentResult(text="agent failed", exit_code=1)
        with patch("runner.drivers.get_driver", return_value=mock_driver):
            result = cmd_refactor(argparse.Namespace(path="some/file.py"))

        assert result == 1
        out = capsys.readouterr().out
        assert "[error]" in out
        assert "agent failed" in out

    def test_resolves_path_relative_to_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        mock_driver = MagicMock()
        mock_driver.run_subagent.return_value = AgentResult(text="findings", exit_code=0)
        with patch("runner.drivers.get_driver", return_value=mock_driver):
            cmd_refactor(argparse.Namespace(path="relative/target.py"))

        call_args = mock_driver.run_subagent.call_args
        assert call_args.kwargs["cwd"] == tmp_path
        assert "relative/target.py" in call_args.args[1]


class TestMainRefactorWiring:
    def test_refactor_subcommand_dispatches_to_cmd_refactor(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["agent", "refactor", "some/file.py"])
        mock_cmd_refactor = MagicMock(return_value=0)
        monkeypatch.setattr(cli, "cmd_refactor", mock_cmd_refactor)

        with pytest.raises(SystemExit) as exc_info:
            cli.main()

        assert exc_info.value.code == 0
        mock_cmd_refactor.assert_called_once()
        assert mock_cmd_refactor.call_args.args[0].path == "some/file.py"
