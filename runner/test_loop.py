"""Tests for runner/loop.py."""

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from runner.drivers.base import AgentResult
from runner.loop import (
    PLAN_FILE,
    PLAN_READY_SIGNAL,
    PLANNER_RETRY_LIMIT,
    REVIEW_APPROVED_SIGNAL,
    REVIEW_CHANGES_SIGNAL,
    REVIEW_RETRY_LIMIT,
    REVIEWER_AGENT,
    SENSOR_RETRY_LIMIT,
    Metrics,
    _append_narrative_outcome,
    _branch_commits,
    _build_narrative,
    _commit_task,
    _main_checkout_dirty_paths,
    _MeteredDriver,
    _offer_merge,
    _parse_tasks,
    _plan_invalid_reason,
    _review_verdict,
    _run_sensors,
    _run_sensors_with_retry,
    _show_diff_in_editor,
    _task_diff,
    _task_title,
    _worker_summary,
    _write_narrative,
    run_loop,
)
from runner.sandbox.base import WorkspaceHandle
from runner.sandbox.noop import NoopSandbox


class _FakeBranchSandbox:
    """Yields a WorkspaceHandle with a truthy branch, without touching real git."""

    def __init__(self, path: Path, branch: str) -> None:
        self._path = path
        self._branch = branch

    @contextmanager
    def workspace(self, project_root: Path):
        yield WorkspaceHandle(path=self._path, branch=self._branch)


def _make_project(tmp_path: Path) -> Path:
    """Scaffold the minimum files run_loop expects in cwd."""
    (tmp_path / "memory").mkdir()
    (tmp_path / "AGENTS.md").write_text("# AGENTS")
    (tmp_path / "memory" / "status.md").write_text("# Status\n")
    return tmp_path


def _read_narrative(tmp_path: Path) -> str:
    """Read the single run-*.md narrative file run_loop wrote under logs/."""
    logs = list((tmp_path / "logs").glob("run-*.md"))
    assert len(logs) == 1, f"expected exactly one narrative file, found {logs}"
    return logs[0].read_text()


def _ok(text="", session_id=None) -> AgentResult:
    return AgentResult(text=text, exit_code=0, session_id=session_id)


def _fail(text="error", session_id=None) -> AgentResult:
    return AgentResult(text=text, exit_code=1, session_id=session_id)


def _plan_then_approve(agent_name, prompt, cwd=None) -> AgentResult:
    """run_subagent side_effect: PLAN READY for the planner, approved for the reviewer."""
    if agent_name == REVIEWER_AGENT:
        return _ok(REVIEW_APPROVED_SIGNAL)
    return _ok(PLAN_READY_SIGNAL)


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

        inner.run_subagent.assert_called_once_with("planner", "prompt", cwd=None)
        assert result == inner.run_subagent.return_value

    def test_run_subagent_forwards_cwd(self):
        inner = MagicMock()
        inner.run_subagent.return_value = _ok("response")
        metrics = Metrics()
        driver = _MeteredDriver(inner, metrics)

        result = driver.run_subagent("reviewer", "prompt", cwd=Path("/tmp"))

        inner.run_subagent.assert_called_once_with(
            "reviewer", "prompt", cwd=Path("/tmp")
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


# ── _plan_invalid_reason ─────────────────────────────────────────────────────

class TestPlanInvalidReason:
    def test_missing_signal_line(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text(MINIMAL_PLAN)
        reason = _plan_invalid_reason("No sign-off here.", str(plan))
        assert reason == f'missing the "{PLAN_READY_SIGNAL} — awaiting approval." line'

    def test_signal_present_but_no_parseable_tasks(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n\n## Context\nno tasks section\n")
        reason = _plan_invalid_reason(
            f"Done.\n{PLAN_READY_SIGNAL} — awaiting approval.", str(plan)
        )
        assert reason == "no parseable numbered ## Tasks section"

    def test_valid_plan_returns_none(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text(MINIMAL_PLAN)
        reason = _plan_invalid_reason(
            f"Done.\n{PLAN_READY_SIGNAL} — awaiting approval.", str(plan)
        )
        assert reason is None


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


class TestRunSensorsWithRetry:
    def _args(self, tmp_path, driver):
        return (tmp_path, 1, 1, "plan.md", "AGENTS.md", "memory/status.md", driver)

    def test_all_pass_returns_no_failures_and_zero_attempts(self, tmp_path):
        sensors = tmp_path / "sensors"
        sensors.mkdir()
        (sensors / "lint.sh").write_text("#!/bin/sh\nexit 0\n")

        driver = MagicMock()

        failures, attempt = _run_sensors_with_retry(*self._args(tmp_path, driver))

        assert failures == []
        assert attempt == 0
        driver.run.assert_not_called()

    def test_fail_once_then_pass_returns_one_attempt(self, tmp_path):
        sensors = tmp_path / "sensors"
        sensors.mkdir()
        lint = sensors / "lint.sh"
        lint.write_text("#!/bin/sh\necho 'lint error' >&2\nexit 1\n")

        def worker_fixes_lint(prompt, context_files, cwd=None):
            lint.write_text("#!/bin/sh\nexit 0\n")
            return _ok()

        driver = MagicMock()
        driver.run.side_effect = worker_fixes_lint

        failures, attempt = _run_sensors_with_retry(*self._args(tmp_path, driver))

        assert failures == []
        assert attempt == 1
        assert driver.run.call_count == 1

    def test_fail_through_budget_returns_remaining_failures(self, tmp_path):
        sensors = tmp_path / "sensors"
        sensors.mkdir()
        (sensors / "lint.sh").write_text("#!/bin/sh\necho 'lint error' >&2\nexit 1\n")

        driver = MagicMock()
        driver.run.return_value = _ok()  # corrective call succeeds but never fixes it

        failures, attempt = _run_sensors_with_retry(*self._args(tmp_path, driver))

        assert len(failures) == 1
        assert failures[0][0] == "lint.sh"
        assert attempt == SENSOR_RETRY_LIMIT
        assert driver.run.call_count == SENSOR_RETRY_LIMIT


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
        driver.run_subagent.side_effect = _plan_then_approve

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
        driver.run_subagent.side_effect = _plan_then_approve

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
        driver.run_subagent.side_effect = _plan_then_approve

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
        driver.run_subagent.side_effect = _plan_then_approve
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
        mock_print.assert_any_call(
            "[metrics] Task 1/1: 2 driver call(s), $0.0000, session None"
        )  # worker + review approval

    def test_sensors_fail_once_then_pass_retries_and_commits(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _plan_then_approve
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
        mock_print.assert_any_call(
            "[metrics] Task 1/1: 3 driver call(s), $0.0000, session None"
        )  # worker + corrective + review approval

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


# ── Review-cycle wiring ───────────────────────────────────────────────────────

class TestRunLoopReviewRetry:
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

    def test_review_approves_first_try_commits_without_corrective(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _plan_then_approve
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task") as mock_commit, \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 0
        assert driver.run_subagent.call_count == 2  # planner + one review call
        assert driver.run.call_count == 1  # only the worker call, no corrective
        mock_commit.assert_called_once()

    def test_review_requests_changes_once_then_approves(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        review_calls = 0

        def run_subagent_side_effect(agent_name, prompt, cwd=None):
            nonlocal review_calls
            if agent_name == REVIEWER_AGENT:
                review_calls += 1
                if review_calls == 1:
                    return _ok(f"{REVIEW_CHANGES_SIGNAL}\nRename `foo` to `bar` in baz.py:12.")
                return _ok(f"{REVIEW_APPROVED_SIGNAL}\nfoo was renamed to bar as requested.")
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task") as mock_commit, \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 0
        assert review_calls == 2
        assert driver.run.call_count == 2  # worker + one review-corrective call
        mock_commit.assert_called_once()

        content = _read_narrative(tmp_path)
        assert "Review: APPROVED — foo was renamed to bar as requested." in content
        assert "Retries: 1 review round" in content  # no sensor retries in this run

    def test_review_requests_changes_every_retry_does_not_fail_closed(
        self, tmp_path, monkeypatch, capsys
    ):
        self._setup(tmp_path, monkeypatch)

        def run_subagent_side_effect(agent_name, prompt, cwd=None):
            if agent_name == REVIEWER_AGENT:
                return _ok(f"{REVIEW_CHANGES_SIGNAL}\nStill not right.")
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        out = capsys.readouterr().out
        # unlike a sensor failure, exhausting the review retry budget never
        # fails closed — the task still commits with the critique recorded
        assert code == 0
        assert driver.run.call_count == 1 + REVIEW_RETRY_LIMIT  # worker + every review-corrective
        mock_commit.assert_called_once()
        assert "[review]" in out
        assert "budget exhausted" in out  # outstanding critique recorded, not discarded

        content = _read_narrative(tmp_path)
        assert "Review: CHANGES REQUESTED (unresolved) — Still not right." in content
        assert f"Retries: {REVIEW_RETRY_LIMIT} review rounds" in content

    def test_sensor_regression_after_review_corrective_still_fails_closed(
        self, tmp_path, monkeypatch
    ):
        self._setup(tmp_path, monkeypatch)

        def run_subagent_side_effect(agent_name, prompt, cwd=None):
            if agent_name == REVIEWER_AGENT:
                return _ok(f"{REVIEW_CHANGES_SIGNAL}\nFix the thing.")
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        sensors_call_count = 0

        def sensors_side_effect(cwd):
            nonlocal sensors_call_count
            sensors_call_count += 1
            if sensors_call_count == 1:
                return []  # passes before the review cycle starts
            return [("lint.sh", "regression")]  # fails on every post-corrective recheck

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", side_effect=sensors_side_effect), \
             patch("runner.loop._commit_task") as mock_commit, \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 1
        mock_commit.assert_not_called()

    def test_sensor_retries_accumulate_across_initial_and_review_corrective_calls(
        self, tmp_path, monkeypatch
    ):
        """
        sensor_retry_count is accumulated across the post-worker call site
        (runner/loop.py's first _run_sensors_with_retry call) and the
        review-corrective-loop call site — this drives one fail-then-pass
        cycle through each and checks the narrative sums them, not just
        records the last one.
        """
        self._setup(tmp_path, monkeypatch)

        review_calls = 0

        def run_subagent_side_effect(agent_name, prompt, cwd=None):
            nonlocal review_calls
            if agent_name == REVIEWER_AGENT:
                review_calls += 1
                if review_calls == 1:
                    return _ok(f"{REVIEW_CHANGES_SIGNAL}\nStill needs a fix.")
                return _ok(f"{REVIEW_APPROVED_SIGNAL}\nAll good now.")
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.side_effect = self._worker_ok(tmp_path)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch(
                 "runner.loop._run_sensors",
                 side_effect=[
                     [("lint.sh", "bad")], [],  # site A: fail once then pass
                     [("lint.sh", "bad")], [],  # site B (inside review loop): fail once then pass
                 ],
             ), \
             patch("runner.loop._commit_task"), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 0
        content = _read_narrative(tmp_path)
        assert "Retries: 2 sensor retries, 1 review round" in content
        assert "Review: APPROVED — All good now." in content


# ── Run narrative wiring ─────────────────────────────────────────────────────

class TestRunLoopNarrative:
    """
    run_loop() feeds _build_narrative()/_write_narrative() from task_narratives
    it assembles internally; _build_narrative and _write_narrative are already
    unit-tested in isolation (TestBuildNarrative, TestWriteNarrative) — these
    tests prove the real loop populates that data correctly end to end.
    """

    def _setup(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(SINGLE_TASK_PLAN)

    def test_narrative_file_reflects_worker_summary_and_review_verdict(
        self, tmp_path, monkeypatch
    ):
        self._setup(tmp_path, monkeypatch)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok("Did the work.\nSUMMARY: Added the config file because tests needed it.")

        def run_subagent_side_effect(agent_name, prompt, cwd=None):
            if agent_name == REVIEWER_AGENT:
                return _ok(f"{REVIEW_APPROVED_SIGNAL}\nConfig matches the plan.")
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task"), \
             patch("builtins.print"):
            code = run_loop("build the thing")

        assert code == 0
        content = _read_narrative(tmp_path)
        assert content.startswith("# Run narrative: build the thing\n")
        assert "## Task 1:" in content
        assert "Add config" in content  # from SINGLE_TASK_PLAN's task title
        assert "Summary: Added the config file because tests needed it." in content
        assert "Review: APPROVED — Config matches the plan." in content
        assert "Retries:" not in content  # zero sensor and review retries → line omitted
        assert "## Outcome" not in content  # NoopSandbox has no branch → _offer_merge never runs

    def test_narrative_records_placeholder_when_worker_omits_summary(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok("Did the work, no marker here.")

        driver = MagicMock()
        driver.run_subagent.side_effect = _plan_then_approve
        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task"), \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 0
        content = _read_narrative(tmp_path)
        assert "Summary: (worker did not provide a summary)" in content


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

        def run_subagent_side_effect(agent_name, prompt, cwd=None):
            if agent_name == REVIEWER_AGENT:
                return _ok(REVIEW_APPROVED_SIGNAL, session_id="sess-review")
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
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
        # run total = planner (1 call)
        #   + task 1 (worker + review approval, no sensor retry = 2 calls)
        #   + task 2 (worker + corrective + review approval, one sensor retry = 3 calls)
        # session id asserted as a real value (not None) so this fails if session
        # tracking gets wired to the wrong source.
        mock_print.assert_any_call(
            "[metrics] Task 1/2: 2 driver call(s), $0.0000, session sess-review"
        )
        mock_print.assert_any_call(
            "[metrics] Task 2/2: 3 driver call(s), $0.0000, session sess-review"
        )
        mock_print.assert_any_call(
            "[metrics] Run total: 6 driver call(s), $0.0000, session sess-review"
        )

        status_text = (tmp_path / "memory" / "status.md").read_text()
        assert "**Run metrics:** 6 driver call(s), $0.0000, session sess-review" in status_text


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


# ── Planner retry loop ──────────────────────────────────────────────────────

class TestRunLoopPlannerRetry:
    def test_valid_plan_no_retry(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        gate = MagicMock()
        # Reject at the gate so the run stops right after validation — the
        # reviewer sub-agent (Phase 2.3) also calls run_subagent per task,
        # which would otherwise inflate call_count beyond the single
        # planner call this test is isolating.
        gate.request.return_value = False

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            run_loop("task")

        assert driver.run_subagent.call_count == 1
        gate.request.assert_called_once_with(PLAN_FILE)

    def test_valid_on_retry_reaches_gate(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan\n\n## Context\nno tasks yet.\n")

        planner_calls = {"n": 0}

        def side_effect(agent_name, prompt, cwd=None):
            if agent_name == REVIEWER_AGENT:
                return _ok(REVIEW_APPROVED_SIGNAL)
            planner_calls["n"] += 1
            if planner_calls["n"] == 1:
                return _ok("no signal here")
            (tmp_path / "plan.md").write_text(MINIMAL_PLAN)
            return _ok(PLAN_READY_SIGNAL)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver = MagicMock()
        driver.run_subagent.side_effect = side_effect
        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("task")

        assert planner_calls["n"] == 2
        gate.request.assert_called_once_with(PLAN_FILE)
        assert code == 0

    def test_invalid_through_full_retry_budget_never_reaches_gate(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan\n\n## Context\nno tasks ever.\n")

        driver = MagicMock()
        driver.run_subagent.return_value = _ok("no signal here")
        gate = MagicMock()

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("task")

        assert driver.run_subagent.call_count == 1 + PLANNER_RETRY_LIMIT
        gate.request.assert_not_called()
        assert code == 1

    def test_retry_attempt_nonzero_exit_fails_closed(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan\n\n## Context\nno tasks yet.\n")

        driver = MagicMock()
        driver.run_subagent.side_effect = [_ok("no signal here"), _fail("crashed")]
        gate = MagicMock()

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("task")

        assert driver.run_subagent.call_count == 2
        gate.request.assert_not_called()
        assert code == 1

    def test_retry_attempt_missing_plan_file_fails_closed(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan\n\n## Context\nno tasks yet.\n")

        calls = {"n": 0}

        def side_effect(agent_name, prompt, cwd=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _ok("no signal here")
            (tmp_path / "plan.md").unlink()
            return _ok("still no signal")

        driver = MagicMock()
        driver.run_subagent.side_effect = side_effect
        gate = MagicMock()

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("builtins.print"):
            code = run_loop("task")

        assert driver.run_subagent.call_count == 2
        gate.request.assert_not_called()
        assert code == 1


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


class TestTaskDiff:
    def _init_repo(self, path: Path) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        (path / "tracked.txt").write_text("original\n")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"],
                       cwd=path, check=True, capture_output=True, env=env)

    def test_modified_tracked_file(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "tracked.txt").write_text("changed\n")
        diff = _task_diff(tmp_path)
        assert "-original" in diff
        assert "+changed" in diff

    def test_new_untracked_file(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("hello\n")
        diff = _task_diff(tmp_path)
        assert "new.txt" in diff
        assert "+hello" in diff

    def test_no_changes_returns_empty_diff(self, tmp_path):
        self._init_repo(tmp_path)
        assert _task_diff(tmp_path) == ""


# ── _review_verdict ──────────────────────────────────────────────────────────

class TestReviewVerdict:
    def test_approved(self):
        approved, critique = _review_verdict(f"Looks good.\n{REVIEW_APPROVED_SIGNAL}")
        assert approved is True
        assert critique == ""

    def test_approved_with_reasoning(self):
        text = f"{REVIEW_APPROVED_SIGNAL}\nThe diff matches the plan and tests cover the new branch."
        approved, critique = _review_verdict(text)
        assert approved is True
        assert critique == "The diff matches the plan and tests cover the new branch."

    def test_changes_requested_with_critique(self):
        text = f"{REVIEW_CHANGES_SIGNAL}\nRename `foo` to `bar` in baz.py:12."
        approved, critique = _review_verdict(text)
        assert approved is False
        assert critique == "Rename `foo` to `bar` in baz.py:12."

    def test_neither_marker_present_falls_back_to_changes_requested(self):
        approved, critique = _review_verdict("  I'm not sure about this.  ")
        assert approved is False
        assert critique == "I'm not sure about this."


# ── _worker_summary ──────────────────────────────────────────────────────────

class TestWorkerSummary:
    def test_summary_present(self):
        text = "Did some work.\nSUMMARY: Added the foo helper because bar needed it."
        assert _worker_summary(text) == "Added the foo helper because bar needed it."

    def test_summary_absent_returns_empty_string(self):
        assert _worker_summary("Did some work, no marker here.") == ""

    def test_multiline_trailing_text_keeps_only_first_line(self):
        text = "Done.\nSUMMARY: Fixed the bug.\nThis extra line should be dropped."
        assert _worker_summary(text) == "Fixed the bug."


# ── _build_narrative ─────────────────────────────────────────────────────────

class TestBuildNarrative:
    def test_empty_task_narratives_is_heading_only(self):
        content = _build_narrative("Add feature X", [])
        assert content == "# Run narrative: Add feature X\n"

    def test_single_task_with_all_fields_populated(self):
        entry = {
            "num": 1,
            "title": "Add the foo helper",
            "summary": "Added the foo helper because bar needed it.",
            "review_approved": True,
            "review_reasoning": "The diff matches the plan.",
            "sensor_retries": 1,
            "review_retries": 1,
        }
        content = _build_narrative("Add feature X", [entry])
        assert content == (
            "# Run narrative: Add feature X\n"
            "\n"
            "## Task 1: Add the foo helper\n"
            "Summary: Added the foo helper because bar needed it.\n"
            "Review: APPROVED — The diff matches the plan.\n"
            "Retries: 1 sensor retry, 1 review round\n"
        )

    def test_zero_retries_omits_retry_line(self):
        entry = {
            "num": 2,
            "title": "Second task",
            "summary": "Did the thing.",
            "review_approved": False,
            "review_reasoning": "Needs more tests.",
            "sensor_retries": 0,
            "review_retries": 0,
        }
        content = _build_narrative("Add feature X", [entry])
        assert content == (
            "# Run narrative: Add feature X\n"
            "\n"
            "## Task 2: Second task\n"
            "Summary: Did the thing.\n"
            "Review: CHANGES REQUESTED (unresolved) — Needs more tests.\n"
        )

    def test_empty_summary_renders_placeholder(self):
        entry = {
            "num": 3,
            "title": "Third task",
            "summary": "",
            "review_approved": True,
            "review_reasoning": "Looks fine.",
            "sensor_retries": 0,
            "review_retries": 0,
        }
        content = _build_narrative("Add feature X", [entry])
        assert "Summary: (worker did not provide a summary)\n" in content


class TestWriteNarrative:
    def test_creates_logs_dir_when_absent(self, tmp_path):
        assert not (tmp_path / "logs").exists()
        _write_narrative(tmp_path, "20260804-131838", "content")
        assert (tmp_path / "logs").is_dir()

    def test_writes_expected_file_content_and_returns_path(self, tmp_path):
        path = _write_narrative(tmp_path, "20260804-131838", "# Run narrative\n")
        assert path == tmp_path / "logs" / "run-20260804-131838.md"
        assert path.read_text() == "# Run narrative\n"

    def test_second_call_with_different_timestamp_does_not_clobber_first(self, tmp_path):
        first = _write_narrative(tmp_path, "20260804-131838", "first content\n")
        second = _write_narrative(tmp_path, "20260804-140000", "second content\n")
        assert first != second
        assert first.read_text() == "first content\n"
        assert second.read_text() == "second content\n"


class TestAppendNarrativeOutcome:
    def test_appends_without_truncating_prior_content(self, tmp_path):
        path = tmp_path / "run-20260804-131838.md"
        path.write_text("# Run narrative\n\n## Task 1: add feature\nSummary: did the thing.\n")

        _append_narrative_outcome(path, "merged")

        content = path.read_text()
        assert content.startswith("# Run narrative\n\n## Task 1: add feature\nSummary: did the thing.\n")
        assert content.endswith("\n## Outcome\nmerged\n")


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

        outcome = _offer_merge("agent/empty", tmp_path)
        out = capsys.readouterr().out
        assert "no commits" in out.lower()
        assert outcome == "no commits"

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
            outcome = _offer_merge("agent/feat", tmp_path, task="add a and b")

        out = capsys.readouterr().out
        assert "squashed" in out.lower()
        assert outcome == "merged"

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
            outcome = _offer_merge("agent/feat", tmp_path)

        out = capsys.readouterr().out
        assert "agent/feat" in out  # instructions mention the branch name
        assert "--squash" in out    # squash-merge instructions, not ff
        assert outcome == "declined"
        branches = subprocess.run(["git", "branch"], cwd=tmp_path,
                                  capture_output=True, text=True, check=False).stdout
        assert "agent/feat" in branches
        # cleanup
        subprocess.run(
            ["git", "branch", "-D", "agent/feat"], cwd=tmp_path, capture_output=True, check=False
        )

    def test_narrative_path_and_zellij_opens_both_diff_and_narrative_panes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("ZELLIJ", "0")
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-b", "agent/feat"],
                       cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "a.txt").write_text("a")
        subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Task 1: add a"],
                       cwd=tmp_path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)

        narrative_path = tmp_path / "logs" / "run-20260804-131838.md"

        with patch("builtins.input", return_value="n"), \
             patch("runner.loop._zellij_edit") as mock_edit:
            outcome = _offer_merge("agent/feat", tmp_path, narrative_path=narrative_path)

        assert outcome == "declined"
        assert mock_edit.call_count == 2
        called_paths = [call.args[0] for call in mock_edit.call_args_list]
        assert str(narrative_path) in called_paths

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

    def test_truthy_branch_passes_narrative_path_and_appends_returned_outcome(
        self, tmp_path, monkeypatch
    ):
        """
        Covers the run_loop() handoff at the handle.branch check: _offer_merge
        must be called with the narrative file's path, and whatever it returns
        must be the exact value passed on to _append_narrative_outcome.
        """
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(SINGLE_TASK_PLAN)

        driver = MagicMock()
        driver.run_subagent.side_effect = _plan_then_approve

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run.side_effect = worker_side_effect
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task"), \
             patch("runner.loop._offer_merge", return_value="merged") as mock_offer, \
             patch("runner.loop._append_narrative_outcome") as mock_append, \
             patch("builtins.print"):
            code = run_loop("task")

        assert code == 0
        mock_offer.assert_called_once()
        args, kwargs = mock_offer.call_args
        assert args[0] == "agent/fake-branch"
        narrative_path = kwargs["narrative_path"]
        assert narrative_path.parent == tmp_path / "logs"
        assert narrative_path.name.startswith("run-") and narrative_path.name.endswith(".md")
        assert narrative_path.exists()
        mock_append.assert_called_once_with(narrative_path, "merged")


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
