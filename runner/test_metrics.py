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


def _tokens(input_tokens=None, read=None, write=None, cost=None) -> AgentResult:
    return AgentResult(
        text="", exit_code=0, cost_usd=cost, input_tokens=input_tokens,
        cache_read_tokens=read, cache_write_tokens=write,
    )


class TestMetricsTokens:
    def test_record_accumulates_token_counters(self):
        metrics = Metrics()

        metrics.record(_tokens(1, 10, 100))
        metrics.record(_tokens(2, 20, 200))

        assert (metrics.input_tokens, metrics.cache_read_tokens, metrics.cache_write_tokens) == (
            3, 30, 300,
        )

    def test_none_counters_skipped_without_affecting_others(self):
        metrics = Metrics()

        metrics.record(_tokens(read=10))
        metrics.record(_tokens(input_tokens=5, write=7))

        assert (metrics.input_tokens, metrics.cache_read_tokens, metrics.cache_write_tokens) == (
            5, 10, 7,
        )
        assert metrics.calls == 2

    def test_record_without_label_updates_totals_only(self):
        metrics = Metrics()

        metrics.record(_tokens(1, 2, 3, cost=0.5))

        assert metrics.by_label == {}
        assert metrics.cache_read_tokens == 2

    def test_per_label_grouping_with_repeated_labels(self):
        metrics = Metrics()

        metrics.record(_tokens(1, 10, 100, cost=0.1), "planner")
        metrics.record(_tokens(2, 20, 200, cost=0.2), "worker")
        metrics.record(_tokens(4, 40, 400, cost=0.4), "worker")
        metrics.record(_tokens(read=5), "worker")

        assert set(metrics.by_label) == {"planner", "worker"}
        planner, worker = metrics.by_label["planner"], metrics.by_label["worker"]
        assert (planner.calls, planner.cost_usd) == (1, pytest.approx(0.1))
        assert (planner.input_tokens, planner.cache_read_tokens, planner.cache_write_tokens) == (
            1, 10, 100,
        )
        assert (worker.calls, worker.cost_usd) == (3, pytest.approx(0.6))
        assert (worker.input_tokens, worker.cache_read_tokens, worker.cache_write_tokens) == (
            6, 65, 600,
        )
        assert metrics.calls == 4

    @pytest.mark.parametrize(
        "input_tokens,read,write,expected",
        [
            pytest.param(100, 300, 600, 0.3, id="normal_ratio"),
            pytest.param(0, 0, 0, None, id="all_zero_is_none"),
            pytest.param(50, 0, 0, 0.0, id="only_input"),
            pytest.param(0, 50, 0, 1.0, id="only_cache_read"),
        ],
    )
    def test_cache_read_share(self, input_tokens, read, write, expected):
        metrics = Metrics()
        metrics.record(_tokens(input_tokens, read, write), "x")

        assert metrics.cache_read_share() == (pytest.approx(expected) if expected is not None else None)
        assert metrics.by_label["x"].cache_read_share() == metrics.cache_read_share()

    def test_cache_read_share_none_with_no_records(self):
        assert Metrics().cache_read_share() is None


class TestMeteredDriverLabels:
    def test_run_subagent_lands_in_agent_name_bucket(self):
        inner = MagicMock()
        inner.run_subagent.return_value = _tokens(1, 2, 3)
        metrics = Metrics()

        _MeteredDriver(inner, metrics).run_subagent("planner", "prompt")

        assert list(metrics.by_label) == ["planner"]
        assert metrics.by_label["planner"].cache_read_tokens == 2

    def test_run_lands_in_corrective_bucket(self):
        inner = MagicMock()
        inner.run.return_value = _tokens(1, 2, 3)
        metrics = Metrics()

        _MeteredDriver(inner, metrics).run("prompt")

        assert list(metrics.by_label) == ["corrective"]
        assert metrics.by_label["corrective"].calls == 1


class TestMeteredDriver:
    def test_run_delegates_args_and_return_value(self):
        inner = MagicMock()
        inner.run.return_value = _ok("response")
        metrics = Metrics()
        driver = _MeteredDriver(inner, metrics)

        result = driver.run("prompt", context_files=["a.md"], cwd=Path("/tmp"))

        inner.run.assert_called_once_with(
            "prompt", context_files=["a.md"], cwd=Path("/tmp"), model=None
        )
        assert result == inner.run.return_value

    def test_run_forwards_model(self):
        inner = MagicMock()
        inner.run.return_value = _ok("response")
        driver = _MeteredDriver(inner, Metrics())

        driver.run("prompt", model="m")

        inner.run.assert_called_once_with("prompt", context_files=None, cwd=None, model="m")

    def test_agent_model_delegates_without_recording(self):
        inner = MagicMock()
        inner.agent_model.return_value = "claude-sonnet-5-5"
        metrics = Metrics()
        driver = _MeteredDriver(inner, metrics)

        assert driver.agent_model("worker") == "claude-sonnet-5-5"
        inner.agent_model.assert_called_once_with("worker")
        assert metrics.calls == 0

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
