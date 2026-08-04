"""Tests for runner/loop.py."""

import os
import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from runner.drivers.base import AgentResult
from runner.loop import (
    PLAN_READY_SIGNAL,
    SENSOR_RETRY_LIMIT,
    Metrics,
    _branch_commits,
    _commit_task,
    _main_checkout_dirty_paths,
    _MeteredDriver,
    _offer_merge,
    _parse_tasks,
    _run_sensors,
    _show_diff_in_editor,
    _task_title,
    run_loop,
)
from runner.sandbox.noop import NoopSandbox


def _make_project(tmp_path: Path) -> Path:
    """Scaffold the minimum files run_loop expects in cwd."""
    (tmp_path / "memory").mkdir()
    (tmp_path / "AGENTS.md").write_text("# AGENTS")
    (tmp_path / "memory" / "status.md").write_text("# Status\n")
    return tmp_path


def _ok(text="") -> AgentResult:
    return AgentResult(text=text, exit_code=0)


def _fail(text="error") -> AgentResult:
    return AgentResult(text=text, exit_code=1)


MINIMAL_PLAN = """\
# Plan: test

## Context
Existing code.

## Tasks

1. **Add config** — create vitest.config.ts
   Files: vitest.config.ts
   What: configure vitest

2. **Add tests** — write scoring.test.ts
   Files: src/lib/scoring.test.ts
   What: table-driven tests for computeScores

## Assumptions
None.

## Risks
None.

## Out of scope
Nothing.
"""

SINGLE_TASK_PLAN = """\
# Plan: test

## Context
Existing code.

## Tasks

1. **Add config** — create vitest.config.ts
   Files: vitest.config.ts
   What: configure vitest

## Assumptions
None.

## Risks
None.

## Out of scope
Nothing.
"""


# ── Metrics / _MeteredDriver ────────────────────────────────────────────────────

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

        inner.run_subagent.assert_called_once_with("planner", "prompt")
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


# ── _parse_tasks ──────────────────────────────────────────────────────────────

class TestParseTasks:
    CASES: ClassVar[list] = [
        pytest.param(
            MINIMAL_PLAN,
            2,
            "1. **Add config**",
            id="two_tasks_extracted",
        ),
        pytest.param(
            "# Plan\n\n## Context\nstuff\n\n## Tasks\n\n1. **Only task** — do it\n   Files: foo.ts\n   What: do it\n",
            1,
            "1. **Only task**",
            id="single_task",
        ),
        pytest.param(
            "# Plan\n\n## Context\nno tasks section\n",
            0,
            None,
            id="no_tasks_section_returns_empty",
        ),
        pytest.param(
            "# Plan\n\n## Tasks\n\nNo numbered items here.\n",
            0,
            None,
            id="tasks_section_but_no_numbered_items",
        ),
    ]

    @pytest.mark.parametrize("content,expected_count,first_start", CASES)
    def test_parse(self, tmp_path, content, expected_count, first_start):
        plan = tmp_path / "plan.md"
        plan.write_text(content)
        tasks = _parse_tasks(str(plan))
        assert len(tasks) == expected_count
        if first_start:
            assert tasks[0].startswith(first_start)

    def test_tasks_contain_files_and_what(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text(MINIMAL_PLAN)
        tasks = _parse_tasks(str(plan))
        assert "vitest.config.ts" in tasks[0]
        assert "scoring.test.ts" in tasks[1]

    def test_each_task_stripped(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text(MINIMAL_PLAN)
        for task in _parse_tasks(str(plan)):
            assert task == task.strip()


# ── _task_title ───────────────────────────────────────────────────────────────

class TestTaskTitle:
    CASES: ClassVar[list] = [
        pytest.param(
            "1. **Add config** — create vitest.config.ts\n   Files: vitest.config.ts",
            "**Add config** — create vitest.config.ts",
            id="strips_numbering",
        ),
        pytest.param(
            "2. **Short**\n   Files: foo.ts",
            "**Short**",
            id="single_word_title",
        ),
        pytest.param(
            "1. " + "x" * 100,
            "x" * 80,
            id="truncated_to_80_chars",
        ),
    ]

    @pytest.mark.parametrize("task_text,expected", CASES)
    def test_title(self, task_text, expected):
        assert _task_title(task_text) == expected


# ── _run_sensors ──────────────────────────────────────────────────────────────

class TestRunSensors:
    def test_all_sensors_pass_returns_empty(self, tmp_path):
        sensors = tmp_path / "sensors"
        sensors.mkdir()
        (sensors / "lint.sh").write_text("#!/bin/sh\nexit 0\n")
        (sensors / "test.sh").write_text("#!/bin/sh\nexit 0\n")

        assert _run_sensors(tmp_path) == []

    def test_one_sensor_fails_captures_name_and_output(self, tmp_path):
        sensors = tmp_path / "sensors"
        sensors.mkdir()
        (sensors / "lint.sh").write_text("#!/bin/sh\necho 'lint error' >&2\nexit 1\n")
        (sensors / "test.sh").write_text("#!/bin/sh\nexit 0\n")

        failures = _run_sensors(tmp_path)

        assert len(failures) == 1
        name, output = failures[0]
        assert name == "lint.sh"
        assert "lint error" in output

    def test_multiple_failures_captured_in_sorted_order(self, tmp_path):
        sensors = tmp_path / "sensors"
        sensors.mkdir()
        (sensors / "b_test.sh").write_text("#!/bin/sh\necho 'b failed'\nexit 1\n")
        (sensors / "a_lint.sh").write_text("#!/bin/sh\necho 'a failed'\nexit 1\n")
        (sensors / "c_ok.sh").write_text("#!/bin/sh\nexit 0\n")

        failures = _run_sensors(tmp_path)

        assert [name for name, _ in failures] == ["a_lint.sh", "b_test.sh"]
        outputs = dict(failures)
        assert "a failed" in outputs["a_lint.sh"]
        assert "b failed" in outputs["b_test.sh"]

    def test_no_sensors_directory_returns_empty(self, tmp_path):
        assert _run_sensors(tmp_path) == []


# ── Planner failures ──────────────────────────────────────────────────────────

class TestRunLoopPlannerFailures:
    CASES: ClassVar[list] = [
        pytest.param(_fail("subprocess error"), False, 1, id="planner_nonzero_exit"),
        pytest.param(_ok(PLAN_READY_SIGNAL), False, 1, id="planner_ok_but_no_plan_file"),
    ]

    @pytest.mark.parametrize("planner_result,write_plan,expected_code", CASES)
    def test_planner_failure(self, tmp_path, monkeypatch, planner_result, write_plan, expected_code):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        if write_plan:
            (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        driver = MagicMock()
        driver.run_subagent.return_value = planner_result
        gate = MagicMock()

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("do something")

        assert code == expected_code
        gate.request.assert_not_called()


# ── No tasks in plan ──────────────────────────────────────────────────────────

class TestRunLoopNoTasks:
    def test_returns_1_when_plan_has_no_tasks_section(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan\n\n## Context\nno tasks.\n")

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 1
        driver.run.assert_not_called()


# ── Gate rejection ────────────────────────────────────────────────────────────

class TestRunLoopGateRejection:
    def test_gate_rejection_returns_2_and_no_worker_called(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        gate = MagicMock()
        gate.request.return_value = False

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 2
        driver.run.assert_not_called()


# ── Per-task execution ────────────────────────────────────────────────────────

class TestRunLoopPerTask:
    def _setup(self, tmp_path, monkeypatch, plan=MINIMAL_PLAN):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(plan)

    def test_worker_called_once_per_task(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 0
        assert driver.run.call_count == 2  # one per task

    def test_each_worker_call_contains_only_its_task(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            run_loop("task")

        first_prompt = driver.run.call_args_list[0][0][0]
        second_prompt = driver.run.call_args_list[1][0][0]
        assert "Add config" in first_prompt
        assert "Add tests" in second_prompt
        # Each call is scoped to its task
        assert "Add tests" not in first_prompt
        assert "Add config" not in second_prompt

    def test_worker_receives_sandbox_cwd(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            run_loop("task")

        # NoopSandbox yields project_root as the workspace path
        for call in driver.run.call_args_list:
            assert call[1]["cwd"] == tmp_path.resolve()

    def test_stops_on_first_task_failure(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.return_value = _fail("task 1 broke")

        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 1
        assert driver.run.call_count == 1  # stopped after first failure

    def test_stops_on_second_task_failure(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)

        call_count = 0

        def worker_side_effect(prompt, context_files, cwd=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                status = tmp_path / "memory" / "status.md"
                status.write_text(status.read_text() + "- task 1 done\n")
                return _ok()
            return _fail("task 2 broke")

        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 1
        assert driver.run.call_count == 2


# ── Sensor retry wiring ──────────────────────────────────────────────────────

class TestRunLoopSensorRetry:
    def _setup(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(SINGLE_TASK_PLAN)

    def _worker_ok(self, tmp_path):
        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()
        return worker_side_effect

    def test_sensors_pass_first_try_commits_without_retry(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task") as mock_commit, \
             patch("builtins.print") as mock_print:
            code = run_loop("task")

        assert code == 0
        assert driver.run.call_count == 1  # only the worker call, no corrective retry
        mock_commit.assert_called_once()
        mock_print.assert_any_call("[metrics] Task 1/1: 1 driver call(s), $0.0000")

    def test_sensors_fail_once_then_pass_retries_and_commits(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", side_effect=[[("lint.sh", "bad")], []]), \
             patch("runner.loop._commit_task") as mock_commit, \
             patch("builtins.print") as mock_print:
            code = run_loop("task")

        assert code == 0
        assert driver.run.call_count == 2  # initial worker call + one corrective call
        mock_commit.assert_called_once()
        mock_print.assert_any_call("[metrics] Task 1/1: 2 driver call(s), $0.0000")

    def test_sensors_fail_every_retry_fails_closed_without_commit(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[("lint.sh", "still bad")]), \
             patch("runner.loop._commit_task") as mock_commit, \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 1
        assert driver.run.call_count == 1 + SENSOR_RETRY_LIMIT  # worker + every corrective retry
        mock_commit.assert_not_called()


# ── Run-level metrics summary ────────────────────────────────────────────────

class TestRunLoopMetricsSummary:
    def test_run_total_printed_and_appended_to_status(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch(
                 "runner.loop._run_sensors",
                 side_effect=[[], [("lint.sh", "bad")], []],
             ), \
             patch("runner.loop._commit_task"), \
             patch("builtins.print") as mock_print:
            code = run_loop("task")

        assert code == 0
        # run total = planner (1 call) + task 1 (1 call, no retry) + task 2 (2 calls, one retry)
        mock_print.assert_any_call("[metrics] Task 1/2: 1 driver call(s), $0.0000")
        mock_print.assert_any_call("[metrics] Task 2/2: 2 driver call(s), $0.0000")
        mock_print.assert_any_call("[metrics] Run total: 4 driver call(s), $0.0000")

        status_text = (tmp_path / "memory" / "status.md").read_text()
        assert "**Run metrics:** 4 driver call(s), $0.0000" in status_text


# ── Main-checkout leak detection ─────────────────────────────────────────────

class TestRunLoopMainCheckoutLeak:
    def _setup(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(SINGLE_TASK_PLAN)

    def _worker_ok(self, tmp_path):
        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()
        return worker_side_effect

    def test_no_leak_prints_no_warning(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._main_checkout_dirty_paths", return_value=[]):
            code = run_loop("task")

        assert code == 0
        out = capsys.readouterr().out
        assert "wrote outside its sandboxed worktree" not in out

    def test_leak_prints_warning_but_does_not_stop_the_loop(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch(
                 "runner.loop._main_checkout_dirty_paths",
                 side_effect=[[], ["runner/loop.py"]],
             ):
            code = run_loop("task")

        assert code == 0  # warns, does not fail the task
        out = capsys.readouterr().out
        assert "wrote outside its sandboxed worktree" in out
        assert "runner/loop.py" in out


# ── Status.md check per task ──────────────────────────────────────────────────

class TestRunLoopStatusCheckPerTask:
    def test_warns_per_task_when_status_not_updated(self, tmp_path, monkeypatch, capsys):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        driver.run.return_value = _ok()  # worker never updates status.md

        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            run_loop("task")

        out = capsys.readouterr().out
        assert out.lower().count("warning") == 2  # one warning per task


# ── PLAN_READY_SIGNAL warning ─────────────────────────────────────────────────

class TestRunLoopPlanReadySignalWarning:
    def test_warns_but_continues_when_signal_missing(self, tmp_path, monkeypatch, capsys):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok("no signal here")

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            code = run_loop("task")

        out = capsys.readouterr().out
        assert "warning" in out.lower()
        assert code == 0


# ── _branch_commits / _offer_merge ────────────────────────────────────────────

def _init_repo(path: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"],
                   cwd=path, check=True, capture_output=True, env=env)


def _make_branch_with_commit(repo: Path, branch: str, message: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(["git", "checkout", "-b", branch], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", message],
                   cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "checkout", "-"], cwd=repo, check=True, capture_output=True)


class TestCommitTask:
    def _init_repo(self, path: Path) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"],
                       cwd=path, check=True, capture_output=True, env=env)

    def test_commits_new_file(self, tmp_path, capsys):
        self._init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("hello")
        _commit_task(1, "Add greeting", tmp_path)
        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                             capture_output=True, text=True, check=False).stdout
        assert "Task 1: Add greeting" in log
        assert "committed" in capsys.readouterr().out.lower()

    def test_nothing_to_commit_skips_silently(self, tmp_path, capsys):
        self._init_repo(tmp_path)
        _commit_task(1, "No changes", tmp_path)
        out = capsys.readouterr().out
        assert "nothing" in out.lower()
        # still only the init commit
        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                             capture_output=True, text=True, check=False).stdout
        assert log.strip().count("\n") == 0  # single commit


class TestMainCheckoutDirtyPaths:
    def _init_repo(self, path: Path) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        (path / "memory").mkdir()
        (path / "memory" / "status.md").write_text("# Status\n")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"],
                       cwd=path, check=True, capture_output=True, env=env)

    def test_clean_checkout_returns_empty(self, tmp_path):
        self._init_repo(tmp_path)
        status_abs = str(tmp_path / "memory" / "status.md")
        assert _main_checkout_dirty_paths(tmp_path, status_abs) == []

    def test_status_md_change_alone_is_excluded(self, tmp_path):
        self._init_repo(tmp_path)
        status_abs = str(tmp_path / "memory" / "status.md")
        (tmp_path / "memory" / "status.md").write_text("# Status\n- did task\n")
        assert _main_checkout_dirty_paths(tmp_path, status_abs) == []

    def test_other_file_change_is_reported(self, tmp_path):
        self._init_repo(tmp_path)
        status_abs = str(tmp_path / "memory" / "status.md")
        (tmp_path / "leaked.py").write_text("# oops\n")
        assert _main_checkout_dirty_paths(tmp_path, status_abs) == ["leaked.py"]

    def test_status_md_and_other_file_reports_only_the_other_file(self, tmp_path):
        self._init_repo(tmp_path)
        status_abs = str(tmp_path / "memory" / "status.md")
        (tmp_path / "memory" / "status.md").write_text("# Status\n- did task\n")
        (tmp_path / "leaked.py").write_text("# oops\n")
        assert _main_checkout_dirty_paths(tmp_path, status_abs) == ["leaked.py"]


class TestBranchCommits:
    def test_returns_commits_ahead_of_head(self, tmp_path):
        _init_repo(tmp_path)
        _make_branch_with_commit(tmp_path, "agent/test", "add feature")
        commits = _branch_commits("agent/test", tmp_path)
        assert len(commits) == 1
        assert "add feature" in commits[0]

    def test_returns_empty_when_no_commits_ahead(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-b", "agent/empty"],
                       cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)
        assert _branch_commits("agent/empty", tmp_path) == []


class TestOfferMerge:
    def test_no_commits_prints_warning_and_returns(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-b", "agent/empty"],
                       cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)

        _offer_merge("agent/empty", tmp_path)
        out = capsys.readouterr().out
        assert "no commits" in out.lower()

    def test_merge_y_squashes_into_one_commit_and_deletes_branch(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        _init_repo(tmp_path)
        # two commits on the branch
        subprocess.run(["git", "checkout", "-b", "agent/feat"],
                       cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "a.txt").write_text("a")
        subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Task 1: add a"],
                       cwd=tmp_path, check=True, capture_output=True, env=env)
        (tmp_path / "b.txt").write_text("b")
        subprocess.run(["git", "add", "b.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Task 2: add b"],
                       cwd=tmp_path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)

        with patch("builtins.input", return_value="y"):
            _offer_merge("agent/feat", tmp_path, task="add a and b")

        out = capsys.readouterr().out
        assert "squashed" in out.lower()

        # branch deleted
        branches = subprocess.run(["git", "branch"], cwd=tmp_path,
                                  capture_output=True, text=True, check=False).stdout
        assert "agent/feat" not in branches

        # main has exactly one new commit (not two)
        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                             capture_output=True, text=True, check=False).stdout.strip().splitlines()
        assert len(log) == 2  # init + one squash commit
        assert "add a and b" in log[0]  # subject is the task description

    def test_merge_n_preserves_branch_and_prints_instructions(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        _init_repo(tmp_path)
        _make_branch_with_commit(tmp_path, "agent/feat", "add feature")

        with patch("builtins.input", return_value="n"):
            _offer_merge("agent/feat", tmp_path)

        out = capsys.readouterr().out
        assert "agent/feat" in out  # instructions mention the branch name
        assert "--squash" in out    # squash-merge instructions, not ff
        branches = subprocess.run(["git", "branch"], cwd=tmp_path,
                                  capture_output=True, text=True, check=False).stdout
        assert "agent/feat" in branches
        # cleanup
        subprocess.run(
            ["git", "branch", "-D", "agent/feat"], cwd=tmp_path, capture_output=True, check=False
        )

    def test_noop_sandbox_skips_merge_offer(self, tmp_path, monkeypatch, capsys):
        """NoopSandbox yields branch=''; run_loop must not call _offer_merge."""
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._offer_merge") as mock_merge:
            run_loop("task")

        mock_merge.assert_not_called()


class TestShowDiffInEditor:
    def test_noop_when_not_in_zellij(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        _init_repo(tmp_path)
        _make_branch_with_commit(tmp_path, "agent/feat", "add feature")

        with patch("runner.loop._zellij_edit") as mock_edit:
            _show_diff_in_editor("agent/feat", tmp_path)

        mock_edit.assert_not_called()

    def test_noop_when_branch_has_no_diff(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ZELLIJ", "0")
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-b", "agent/empty"],
                       cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)

        with patch("runner.loop._zellij_edit") as mock_edit:
            _show_diff_in_editor("agent/empty", tmp_path)

        mock_edit.assert_not_called()

    def test_opens_editor_with_diff_file_when_in_zellij(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ZELLIJ", "0")
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-b", "agent/feat"],
                       cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "feature.txt").write_text("new feature\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add feature"],
                       cwd=tmp_path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)

        with patch("runner.loop._zellij_edit") as mock_edit:
            _show_diff_in_editor("agent/feat", tmp_path)

        mock_edit.assert_called_once()
        diff_path = Path(mock_edit.call_args[0][0])
        assert diff_path.suffix == ".diff"
        assert "feature.txt" in diff_path.read_text()
