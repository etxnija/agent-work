"""Tests for runner/loop.py."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from runner.drivers.base import AgentResult
from runner.loop import PLAN_READY_SIGNAL, run_loop


def _make_project(tmp_path: Path) -> Path:
    """Scaffold the minimum files run_loop expects to find in cwd."""
    (tmp_path / "memory").mkdir()
    (tmp_path / "AGENTS.md").write_text("# AGENTS")
    (tmp_path / "memory" / "status.md").write_text("# Status\n")
    return tmp_path


def _ok(text="") -> AgentResult:
    return AgentResult(text=text, exit_code=0)


def _fail(text="error") -> AgentResult:
    return AgentResult(text=text, exit_code=1)


# ── Planner failures ──────────────────────────────────────────────────────────

class TestRunLoopPlannerFailures:
    CASES = [
        pytest.param(
            _fail("subprocess error"),
            False,   # plan.md written?
            1,
            id="planner_nonzero_exit",
        ),
        pytest.param(
            _ok(f"{PLAN_READY_SIGNAL}"),
            False,   # plan.md NOT written — loop should catch this
            1,
            id="planner_ok_but_no_plan_file",
        ),
    ]

    @pytest.mark.parametrize("planner_result,write_plan,expected_code", CASES)
    def test_planner_failure(self, tmp_path, monkeypatch, planner_result, write_plan, expected_code):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        driver = MagicMock()
        driver.run_subagent.return_value = planner_result
        gate = MagicMock()

        if write_plan:
            (tmp_path / "plan.md").write_text("# Plan")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("builtins.print"):
            code = run_loop("do something")

        assert code == expected_code
        gate.request.assert_not_called()


class TestRunLoopPlanReadySignalWarning:
    def test_warns_but_continues_when_signal_missing(self, tmp_path, monkeypatch, capsys):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan")

        driver = MagicMock()
        driver.run_subagent.return_value = _ok("no signal here")
        driver.run.return_value = _ok()

        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate):
            code = run_loop("task")

        out = capsys.readouterr().out
        assert "warning" in out.lower()
        assert code == 0


# ── Gate rejection ────────────────────────────────────────────────────────────

class TestRunLoopGateRejection:
    def test_gate_rejection_returns_2(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan")

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)

        gate = MagicMock()
        gate.request.return_value = False

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 2
        driver.run.assert_not_called()


# ── Worker failures ───────────────────────────────────────────────────────────

class TestRunLoopWorkerFailure:
    def test_worker_nonzero_exit_returns_1(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan")

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.return_value = _fail("worker blew up")

        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 1


# ── Success path ──────────────────────────────────────────────────────────────

class TestRunLoopSuccess:
    def test_success_returns_0(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan")

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)

        def worker_side_effect(prompt, context_files):
            # Simulate worker updating status.md
            (tmp_path / "memory" / "status.md").write_text("# Status\n\n## done\n")
            return _ok("done")

        driver.run.side_effect = worker_side_effect

        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 0

    def test_worker_receives_plan_and_agents_as_context(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan")

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)

        def worker_side_effect(prompt, context_files):
            (tmp_path / "memory" / "status.md").write_text("updated")
            return _ok()

        driver.run.side_effect = worker_side_effect

        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("builtins.print"):
            run_loop("task")

        _, kwargs = driver.run.call_args
        context_files = kwargs.get("context_files") or driver.run.call_args[0][1]
        assert "plan.md" in context_files
        assert "AGENTS.md" in context_files


# ── Status.md update check ────────────────────────────────────────────────────

class TestRunLoopStatusCheck:
    def test_warns_when_status_not_updated(self, tmp_path, monkeypatch, capsys):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan")

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.return_value = _ok()  # worker does NOT update status.md

        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate):
            run_loop("task")

        out = capsys.readouterr().out
        assert "status" in out.lower()
        assert "warning" in out.lower()
