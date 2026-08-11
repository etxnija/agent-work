"""Tests for runner/metrics.py."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from runner.drivers.base import AgentResult
from runner.metrics import Metrics, _MeteredDriver


def _ok(text="", session_id=None) -> AgentResult:
    return AgentResult(text=text, exit_code=0, session_id=session_id)


def _fail(text="error", session_id=None) -> AgentResult:
    return AgentResult(text=text, exit_code=1, session_id=session_id)


class TestMetrics:
    def test_record_increments_calls_and_adds_cost(self):
        metrics = Metrics()

        metrics.record(AgentResult(text="a", exit_code=0, cost_usd=0.01))
        metrics.record(AgentResult(text="b", exit_code=0, cost_usd=0.02))

        assert metrics.calls == 2
        assert metrics.cost_usd == pytest.approx(0.03)

    def test_record_with_none_cost_increments_calls_only(self):
        metrics = Metrics()

        metrics.record(AgentResult(text="a", exit_code=0, cost_usd=None))

        assert metrics.calls == 1
        assert metrics.cost_usd == 0.0

    def test_record_updates_last_session_id_to_most_recent(self):
        metrics = Metrics()

        metrics.record(AgentResult(text="a", exit_code=0, session_id="sess-1"))
        metrics.record(AgentResult(text="b", exit_code=0, session_id="sess-2"))

        assert metrics.last_session_id == "sess-2"

    def test_record_with_none_session_id_leaves_prior_value_untouched(self):
        metrics = Metrics()

        metrics.record(AgentResult(text="a", exit_code=0, session_id="sess-1"))
        metrics.record(AgentResult(text="b", exit_code=0, session_id=None))

        assert metrics.last_session_id == "sess-1"


class TestMeteredDriver:
    def test_run_delegates_args_and_return_value(self):
        inner = MagicMock()
        inner.run.return_value = _ok("response")
        metrics = Metrics()
        driver = _MeteredDriver(inner, metrics)

        result = driver.run("prompt", context_files=["a.md"], cwd=Path("/tmp"))

        inner.run.assert_called_once_with(
            "prompt", context_files=["a.md"], cwd=Path("/tmp")
        )
        assert result == inner.run.return_value

    def test_run_subagent_delegates_args_and_return_value(self):
        inner = MagicMock()
        inner.run_subagent.return_value = _ok("response")
        metrics = Metrics()
        driver = _MeteredDriver(inner, metrics)

        result = driver.run_subagent("planner", "prompt")

        inner.run_subagent.assert_called_once_with(
            "planner", "prompt", context_files=None, cwd=None
        )
        assert result == inner.run_subagent.return_value

    def test_run_subagent_forwards_cwd(self):
        inner = MagicMock()
        inner.run_subagent.return_value = _ok("response")
        metrics = Metrics()
        driver = _MeteredDriver(inner, metrics)

        result = driver.run_subagent("reviewer", "prompt", cwd=Path("/tmp"))

        inner.run_subagent.assert_called_once_with(
            "reviewer", "prompt", context_files=None, cwd=Path("/tmp")
        )
        assert result == inner.run_subagent.return_value

    def test_run_records_call_and_cost_into_metrics(self):
        inner = MagicMock()
        inner.run.return_value = AgentResult(text="x", exit_code=0, cost_usd=0.05)
        metrics = Metrics()
        driver = _MeteredDriver(inner, metrics)

        driver.run("prompt")

        assert metrics.calls == 1
        assert metrics.cost_usd == pytest.approx(0.05)

    def test_run_subagent_records_call_and_cost_into_metrics(self):
        inner = MagicMock()
        inner.run_subagent.return_value = AgentResult(text="x", exit_code=0, cost_usd=0.05)
        metrics = Metrics()
        driver = _MeteredDriver(inner, metrics)

        driver.run_subagent("planner", "prompt")

        assert metrics.calls == 1
        assert metrics.cost_usd == pytest.approx(0.05)

    def test_run_with_none_cost_increments_calls_without_touching_cost(self):
        inner = MagicMock()
        inner.run.return_value = _ok("response")
        metrics = Metrics()
        driver = _MeteredDriver(inner, metrics)

        driver.run("prompt")

        assert metrics.calls == 1
        assert metrics.cost_usd == 0.0
