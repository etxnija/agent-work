"""Tests for runner/loop.py."""

import json
import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from runner.drivers.base import AgentResult
from runner.loop import (
    CODE_HEALTH_RETRY_LIMIT,
    PLAN_FILE,
    PLAN_READY_SIGNAL,
    PLANNER_RETRY_LIMIT,
    REVIEW_APPROVED_SIGNAL,
    REVIEW_CHANGES_SIGNAL,
    REVIEW_RETRY_LIMIT,
    REVIEWER_AGENT,
    SENSOR_RETRY_LIMIT,
    WORKER_AGENT,
    _branch_commits,
    _commit_status_update,
    _commit_task,
    _log_handlers,
    _main_checkout_dirty_paths,
    _offer_merge,
    _parse_task_concepts,
    _parse_tasks,
    _perform_squash_merge,
    _plan_invalid_reason,
    _review_verdict,
    _run_code_health_with_retry,
    _run_review_with_retry,
    _run_sensors,
    _run_sensors_with_retry,
    _show_diff_in_editor,
    _stamp_verified,
    _task_diff,
    _task_title,
    _update_coverage_baseline,
    _worker_summary,
    _write_last_run_state,
    run_loop,
)
from runner.loop import (
    logger as loop_logger,
)
from runner.sandbox.base import WorkspaceHandle
from runner.sandbox.noop import NoopSandbox


class _FakeBranchSandbox:
    """Yields a WorkspaceHandle with a truthy branch, without touching real git."""

    def __init__(self, path: Path, branch: str) -> None:
        self._path = path
        self._branch = branch
        self.handle: WorkspaceHandle | None = None

    @contextmanager
    def workspace(self, project_root: Path):
        self.handle = WorkspaceHandle(path=self._path, branch=self._branch)
        yield self.handle


def _make_project(tmp_path: Path) -> Path:
    """Scaffold the minimum files run_loop expects in cwd."""
    (tmp_path / "memory").mkdir()
    (tmp_path / "AGENTS.md").write_text("# AGENTS")
    (tmp_path / "memory" / "status.md").write_text("# Status\n")
    return tmp_path


def _ok(text="", session_id=None) -> AgentResult:
    return AgentResult(text=text, exit_code=0, session_id=session_id)


def _fail(text="error", session_id=None) -> AgentResult:
    return AgentResult(text=text, exit_code=1, session_id=session_id)


def _route_subagent(worker=None):
    """
    run_subagent side_effect factory: PLAN READY for the planner, approved for
    the reviewer, and delegates to `worker` (or a plain _ok()) for the worker.
    """
    def _route(agent_name, prompt, context_files=None, cwd=None) -> AgentResult:
        if agent_name == REVIEWER_AGENT:
            return _ok(REVIEW_APPROVED_SIGNAL)
        if agent_name == WORKER_AGENT:
            if worker is not None:
                return worker(prompt, context_files=context_files, cwd=cwd)
            return _ok()
        return _ok(PLAN_READY_SIGNAL)
    return _route


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


# ── _log_handlers ────────────────────────────────────────────────────────────

class TestLogHandlers:
    def test_attaches_and_detaches_handlers(self, tmp_path):
        log_path = tmp_path / "run.log"
        before = set(loop_logger.handlers)

        with _log_handlers(log_path):
            during = set(loop_logger.handlers) - before
            assert len(during) == 2
            kinds = {type(h) for h in during}
            assert kinds == {logging.StreamHandler, logging.FileHandler}
            loop_logger.info("hello from the loop")

        assert set(loop_logger.handlers) == before
        assert "hello from the loop" in log_path.read_text()

    def test_removes_handlers_on_exception(self, tmp_path):
        log_path = tmp_path / "run.log"
        before = set(loop_logger.handlers)

        with pytest.raises(ValueError), _log_handlers(log_path):
            raise ValueError("boom")

        assert set(loop_logger.handlers) == before

    def test_console_level_gates_debug_but_not_info(self, tmp_path, caplog):
        log_path = tmp_path / "run.log"
        with _log_handlers(log_path):
            caplog.set_level(logging.INFO, logger="agent_loop")
            loop_logger.debug("debug detail")
            assert "debug detail" not in caplog.text

            caplog.set_level(logging.DEBUG, logger="agent_loop")
            loop_logger.debug("debug detail")
            assert "debug detail" in caplog.text

    def test_file_handler_receives_debug_with_timestamp(self, tmp_path):
        log_path = tmp_path / "run.log"
        with _log_handlers(log_path):
            loop_logger.debug("debug detail")

        content = log_path.read_text()
        line = next(l for l in content.splitlines() if "debug detail" in l)
        assert line != "debug detail"  # a timestamp prefix was added
        assert line.split(" ", 1)[0][:4].isdigit()  # prefix starts with a year

    def test_repeated_run_loop_calls_do_not_accumulate_handlers(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        gate = MagicMock()
        gate.request.return_value = False  # stop right after plan validation

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            assert len(loop_logger.handlers) == 0
            run_loop("task")
            assert len(loop_logger.handlers) == 0
            run_loop("task")
            assert len(loop_logger.handlers) == 0


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


# ── _parse_task_concepts ──────────────────────────────────────────────────────

class TestParseTaskConcepts:
    def test_no_concepts_line_returns_empty(self, tmp_path):
        task_text = "1. **Task** — do stuff\n   Files: foo.py\n   What: stuff"
        assert _parse_task_concepts(task_text, tmp_path) == []

    def test_existing_concept_file_parsed(self, tmp_path):
        concepts = tmp_path / "memory" / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "sandboxing.md").write_text("content")

        task_text = (
            "1. **Fix sandbox** — update worktree\n"
            "   Files: runner/loop.py\n"
            "   Concepts: sandboxing.md\n"
            "   What: update"
        )
        parsed = _parse_task_concepts(task_text, tmp_path)
        assert len(parsed) == 1
        assert parsed[0] == str(concepts / "sandboxing.md")

    def test_missing_concept_file_skipped(self, tmp_path):
        concepts = tmp_path / "memory" / "concepts"
        concepts.mkdir(parents=True)

        task_text = "1. **Task**\n   Concepts: non_existent.md"
        assert _parse_task_concepts(task_text, tmp_path) == []

    def test_multiple_comma_separated_concepts(self, tmp_path):
        concepts = tmp_path / "memory" / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "c1.md").write_text("c1")
        (concepts / "c2.md").write_text("c2")

        task_text = "1. **Task**\n   Concepts: c1.md, c2.md"
        parsed = _parse_task_concepts(task_text, tmp_path)
        assert len(parsed) == 2
        assert str(concepts / "c1.md") in parsed
        assert str(concepts / "c2.md") in parsed

    def test_tag_matching_fallback_when_concepts_line_omitted(self, tmp_path):
        concepts = tmp_path / "memory" / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "sandboxing.md").write_text("---\ntags: [sandboxing, worktree]\n---\ncontent")
        (concepts / "metrics.md").write_text("---\ntags: [telemetry, cost]\n---\ncontent")

        task_text = (
            "1. **Fix worktree leak** — update sandbox code\n"
            "   Files: runner/loop.py\n"
            "   What: fix worktree path comparison"
        )
        parsed = _parse_task_concepts(task_text, tmp_path)
        assert len(parsed) == 1
        assert parsed[0] == str(concepts / "sandboxing.md")

    def test_explicit_concepts_line_disables_fallback(self, tmp_path):
        concepts = tmp_path / "memory" / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "sandboxing.md").write_text("---\ntags: [sandboxing, worktree]\n---\ncontent")
        (concepts / "metrics.md").write_text("---\ntags: [telemetry, cost]\n---\ncontent")

        task_text = (
            "1. **Fix worktree leak** — update sandbox code\n"
            "   Files: runner/loop.py\n"
            "   Concepts: metrics.md\n"
            "   What: update metrics"
        )
        parsed = _parse_task_concepts(task_text, tmp_path)
        assert len(parsed) == 1
        assert parsed[0] == str(concepts / "metrics.md")


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

    def test_short_circuits_at_first_failure(self, tmp_path):
        sensors = tmp_path / "sensors"
        sensors.mkdir()
        marker = tmp_path / "b_ran"
        (sensors / "a_lint.sh").write_text("#!/bin/sh\necho 'a failed'\nexit 1\n")
        (sensors / "b_test.sh").write_text(
            f"#!/bin/sh\ntouch {marker}\necho 'b failed'\nexit 1\n"
        )
        (sensors / "c_ok.sh").write_text("#!/bin/sh\nexit 0\n")

        failures = _run_sensors(tmp_path)

        assert [name for name, _ in failures] == ["a_lint.sh"]
        outputs = dict(failures)
        assert "a failed" in outputs["a_lint.sh"]
        assert not marker.exists()

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

    def test_two_failures_surfaced_serially_share_one_budget(self, tmp_path):
        sensors = tmp_path / "sensors"
        sensors.mkdir()
        a_lint = sensors / "a_lint.sh"
        b_test = sensors / "b_test.sh"
        a_lint.write_text("#!/bin/sh\necho 'a failed'\nexit 1\n")
        b_test.write_text("#!/bin/sh\necho 'b failed'\nexit 1\n")

        def fix_currently_reported(prompt, context_files, cwd=None):
            if "a_lint.sh" in prompt:
                a_lint.write_text("#!/bin/sh\nexit 0\n")
            elif "b_test.sh" in prompt:
                b_test.write_text("#!/bin/sh\nexit 0\n")
            return _ok()

        driver = MagicMock()
        driver.run.side_effect = fix_currently_reported

        failures, attempt = _run_sensors_with_retry(*self._args(tmp_path, driver))

        assert failures == []
        assert attempt == 2
        assert driver.run.call_count == 2


class TestRunCodeHealthWithRetry:
    def _args(self, tmp_path, driver):
        return (tmp_path, 1, 1, "plan.md", "AGENTS.md", "memory/status.md", driver)

    @patch("runner.loop.check_code_health")
    def test_no_findings_returns_empty_and_zero_attempts(self, mock_check, tmp_path):
        mock_check.return_value = []
        driver = MagicMock()

        findings, attempt = _run_code_health_with_retry(*self._args(tmp_path, driver))

        assert findings == []
        assert attempt == 0
        driver.run.assert_not_called()

    @patch("runner.loop.check_code_health")
    def test_findings_fixed_after_one_corrective_call(self, mock_check, tmp_path):
        mock_check.side_effect = [["foo.py: bar (lines 1-60) — NLOC 60 exceeds threshold 50"], []]
        driver = MagicMock()
        driver.run.return_value = _ok()

        findings, attempt = _run_code_health_with_retry(*self._args(tmp_path, driver))

        assert findings == []
        assert attempt == 1
        assert driver.run.call_count == 1

    @patch("runner.loop.check_code_health")
    def test_findings_persist_through_full_budget(self, mock_check, tmp_path):
        mock_check.return_value = ["foo.py: bar (lines 1-60) — NLOC 60 exceeds threshold 50"]
        driver = MagicMock()
        driver.run.return_value = _ok()

        findings, attempt = _run_code_health_with_retry(*self._args(tmp_path, driver))

        assert findings == ["foo.py: bar (lines 1-60) — NLOC 60 exceeds threshold 50"]
        assert attempt == CODE_HEALTH_RETRY_LIMIT
        assert driver.run.call_count == CODE_HEALTH_RETRY_LIMIT

    @patch("runner.loop.check_code_health")
    def test_corrective_call_failure_breaks_retry_loop(self, mock_check, tmp_path):
        mock_check.return_value = ["foo.py: bar (lines 1-60) — NLOC 60 exceeds threshold 50"]
        driver = MagicMock()
        driver.run.return_value = _fail()

        findings, attempt = _run_code_health_with_retry(*self._args(tmp_path, driver))

        assert findings == ["foo.py: bar (lines 1-60) — NLOC 60 exceeds threshold 50"]
        assert attempt == 1
        assert driver.run.call_count == 1


class TestRunReviewWithRetry:
    def _args(self, tmp_path, driver, review_critiques):
        return (
            tmp_path, "task text", 1, 1,
            "plan.md", "AGENTS.md", "memory/status.md",
            driver, review_critiques,
        )

    def test_approve_on_first_review_no_corrective(self, tmp_path):
        driver = MagicMock()
        driver.run_subagent.return_value = _ok(f"{REVIEW_APPROVED_SIGNAL}\nLooks good.")
        review_critiques: dict[int, str] = {}

        result = _run_review_with_retry(*self._args(tmp_path, driver, review_critiques))

        assert result == (True, "Looks good.", 0, 0, [])
        driver.run.assert_not_called()
        assert 1 not in review_critiques

    def test_changes_requested_once_then_approved(self, tmp_path):
        review_responses = iter([
            _ok(f"{REVIEW_CHANGES_SIGNAL}\nFix the thing."),
            _ok(f"{REVIEW_APPROVED_SIGNAL}\nFixed."),
        ])
        driver = MagicMock()
        driver.run_subagent.side_effect = lambda *a, **k: next(review_responses)
        driver.run.return_value = _ok()
        review_critiques: dict[int, str] = {}

        approved, critique, review_attempt, sensor_retry_count, failures = (
            _run_review_with_retry(*self._args(tmp_path, driver, review_critiques))
        )

        assert approved is True
        assert critique == "Fixed."
        assert review_attempt == 1
        assert sensor_retry_count == 0
        assert failures == []
        assert driver.run.call_count == 1
        assert 1 not in review_critiques

    def test_changes_requested_through_full_budget_does_not_fail_closed(self, tmp_path):
        driver = MagicMock()
        driver.run_subagent.return_value = _ok(f"{REVIEW_CHANGES_SIGNAL}\nStill not right.")
        driver.run.return_value = _ok()
        review_critiques: dict[int, str] = {}

        approved, critique, review_attempt, _sensor_retry_count, failures = (
            _run_review_with_retry(*self._args(tmp_path, driver, review_critiques))
        )

        assert approved is False
        assert critique == "Still not right."
        assert review_attempt == REVIEW_RETRY_LIMIT
        assert failures == []
        assert driver.run.call_count == REVIEW_RETRY_LIMIT
        assert review_critiques[1] == "Still not right."

    def test_sensor_regression_on_post_corrective_recheck_fails_closed(self, tmp_path):
        sensors = tmp_path / "sensors"
        sensors.mkdir()
        (sensors / "lint.sh").write_text("#!/bin/sh\necho 'regression' >&2\nexit 1\n")

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(f"{REVIEW_CHANGES_SIGNAL}\nFix the thing.")
        driver.run.return_value = _ok()
        review_critiques: dict[int, str] = {}

        approved, _critique, review_attempt, _sensor_retry_count, failures = (
            _run_review_with_retry(*self._args(tmp_path, driver, review_critiques))
        )

        assert approved is False
        assert review_attempt == 1
        assert failures != []
        assert failures[0][0] == "lint.sh"
        assert 1 not in review_critiques

    def test_corrective_call_failure_breaks_retry_loop(self, tmp_path):
        driver = MagicMock()
        driver.run_subagent.return_value = _ok(f"{REVIEW_CHANGES_SIGNAL}\nFix the thing.")
        driver.run.return_value = _fail()
        review_critiques: dict[int, str] = {}

        approved, critique, review_attempt, sensor_retry_count, failures = (
            _run_review_with_retry(*self._args(tmp_path, driver, review_critiques))
        )

        assert approved is False
        assert critique == "Fix the thing."
        assert review_attempt == 1
        assert sensor_retry_count == 0
        assert failures == []
        assert driver.run.call_count == 1
        assert review_critiques[1] == "Fix the thing."


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
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
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
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            code = run_loop("task")

        assert code == 1
        driver.run.assert_not_called()

    def test_returns_1_when_tasks_removed_during_approval(self, tmp_path, monkeypatch):
        """Human can edit plan.md at the approval gate; re-parse after approval must catch it."""
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        def _strip_tasks_and_approve(_plan_file):
            (tmp_path / "plan.md").write_text("# Plan\n\n## Context\nno tasks.\n")
            return True

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        gate = MagicMock()
        gate.request.side_effect = _strip_tasks_and_approve

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
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
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            code = run_loop("task")

        assert code == 2
        driver.run.assert_not_called()

    def test_gate_rejection_commits_status_update(self, tmp_path, monkeypatch):
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
             patch("runner.loop._commit_status_update") as commit_status_update:
            run_loop("task")

        commit_status_update.assert_called_once_with(
            "Record plan-rejected status", tmp_path.resolve()
        )

    def test_gate_rejection_leaves_no_dirty_status_md(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        _make_project(tmp_path)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        subprocess.run(["git", "commit", "-m", "scaffold project"], cwd=tmp_path,
                       check=True, capture_output=True, env=env)

        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        driver = MagicMock()
        driver.run_subagent.return_value = _ok(PLAN_READY_SIGNAL)
        gate = MagicMock()
        gate.request.return_value = False

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            code = run_loop("task")

        assert code == 2
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "memory/status.md"],
            cwd=tmp_path, capture_output=True, text=True, check=False,
        ).stdout
        assert status == ""


# ── Per-task execution ────────────────────────────────────────────────────────

class TestRunLoopPerTask:
    def _setup(self, tmp_path, monkeypatch, plan=MINIMAL_PLAN):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(plan)

    def test_worker_called_once_per_task(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run_subagent.side_effect = _route_subagent(worker_side_effect)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            code = run_loop("task")

        assert code == 0
        worker_calls = [c for c in driver.run_subagent.call_args_list if c.args[0] == WORKER_AGENT]
        assert len(worker_calls) == 2  # one per task
        driver.run.assert_not_called()

    def test_fresh_task_invokes_run_subagent_with_worker_agent(self, tmp_path, monkeypatch):
        """
        The fresh-task call site must go through driver.run_subagent(WORKER_AGENT, ...),
        not the raw driver.run() — and the prompt must no longer carry a manually
        prepended static-instructions preamble, since run_subagent injects the
        worker's agent body (agents/worker.md) automatically.
        """
        self._setup(tmp_path, monkeypatch, plan=SINGLE_TASK_PLAN)

        driver = MagicMock()

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run_subagent.side_effect = _route_subagent(worker_side_effect)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            code = run_loop("task")

        assert code == 0
        driver.run.assert_not_called()
        worker_calls = [c for c in driver.run_subagent.call_args_list if c.args[0] == WORKER_AGENT]
        assert len(worker_calls) == 1
        worker_prompt = worker_calls[0].args[1]
        assert "Task to implement from" in worker_prompt
        assert (
            "You are an autonomous software engineering worker implementing a single task."
            not in worker_prompt
        )

    def test_each_worker_call_contains_only_its_task(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run_subagent.side_effect = _route_subagent(worker_side_effect)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            run_loop("task")

        worker_calls = [c for c in driver.run_subagent.call_args_list if c.args[0] == WORKER_AGENT]
        first_prompt = worker_calls[0].args[1]
        second_prompt = worker_calls[1].args[1]
        assert "Add config" in first_prompt
        assert "Add tests" in second_prompt
        # Each call is scoped to its task
        assert "Add tests" not in first_prompt
        assert "Add config" not in second_prompt

    def test_worker_receives_sandbox_cwd(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run_subagent.side_effect = _route_subagent(worker_side_effect)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            run_loop("task")

        # NoopSandbox yields project_root as the workspace path
        worker_calls = [c for c in driver.run_subagent.call_args_list if c.args[0] == WORKER_AGENT]
        assert worker_calls
        for call in worker_calls:
            assert call.kwargs["cwd"] == tmp_path.resolve()

    def test_stops_on_first_task_failure(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(lambda *a, **k: _fail("task 1 broke"))

        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            code = run_loop("task")

        assert code == 1
        worker_calls = [c for c in driver.run_subagent.call_args_list if c.args[0] == WORKER_AGENT]
        assert len(worker_calls) == 1  # stopped after first failure

    def test_worker_hard_failure_preserves_branch(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(lambda *a, **k: _fail("task 1 broke"))

        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox):
            code = run_loop("task")

        assert code == 1
        assert fake_sandbox.handle is not None
        assert fake_sandbox.handle._keep is True
        assert any(
            "agent/fake-branch" in line and "0 completed task(s)" in line
            for line in caplog.messages
        )
        state_file = tmp_path / ".agent-last-run.json"
        assert state_file.exists()
        assert json.loads(state_file.read_text())["branch"] == "agent/fake-branch"

    def test_second_task_worker_failure_preserves_first_tasks_commit(
        self, tmp_path, monkeypatch, caplog
    ):
        """Two-task plan: task 1 completes, task 2's worker call fails —
        the branch is preserved and the recovery message names 1 completed task."""
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()

        call_count = 0

        def worker_side_effect(prompt, context_files, cwd=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                status = tmp_path / "memory" / "status.md"
                status.write_text(status.read_text() + "- task 1 done\n")
                return _ok()
            return _fail("task 2 broke")

        driver.run_subagent.side_effect = _route_subagent(worker_side_effect)
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 1
        assert fake_sandbox.handle is not None
        assert fake_sandbox.handle._keep is True
        assert any(
            "agent/fake-branch" in line and "1 completed task(s)" in line
            for line in caplog.messages
        )
        mock_commit.assert_called_once()

    def test_stops_on_second_task_failure(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()

        call_count = 0

        def worker_side_effect(prompt, context_files, cwd=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                status = tmp_path / "memory" / "status.md"
                status.write_text(status.read_text() + "- task 1 done\n")
                return _ok()
            return _fail("task 2 broke")

        driver.run_subagent.side_effect = _route_subagent(worker_side_effect)
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            code = run_loop("task")

        assert code == 1
        worker_calls = [c for c in driver.run_subagent.call_args_list if c.args[0] == WORKER_AGENT]
        assert len(worker_calls) == 2

    def test_successful_run_writes_last_run_state(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(SINGLE_TASK_PLAN)

        driver = MagicMock()

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run_subagent.side_effect = _route_subagent(worker_side_effect)
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._commit_task"), \
             patch("runner.loop._offer_merge", return_value="merged"):
            code = run_loop("task")

        assert code == 0
        assert fake_sandbox.handle is not None
        assert fake_sandbox.handle._keep is True
        state_file = tmp_path / ".agent-last-run.json"
        assert state_file.exists()
        assert json.loads(state_file.read_text())["branch"] == "agent/fake-branch"


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

    def test_sensors_pass_first_try_commits_without_retry(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 0
        driver.run.assert_not_called()  # no corrective retry
        mock_commit.assert_called_once()
        assert (
            "[metrics] Task 1/1: 2 driver call(s), $0.0000, session None" in caplog.messages
        )  # worker + review approval

    def test_sensors_fail_once_then_pass_retries_and_commits(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        driver.run.return_value = _ok()  # corrective call succeeds
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", side_effect=[[("lint.sh", "bad")], []]), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 0
        assert driver.run.call_count == 1  # one corrective call (worker call now goes through run_subagent)
        mock_commit.assert_called_once()
        assert (
            "[metrics] Task 1/1: 3 driver call(s), $0.0000, session None" in caplog.messages
        )  # worker + corrective + review approval

    def test_sensors_fail_every_retry_fails_closed_without_commit(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        driver.run.return_value = _ok()  # corrective call succeeds but never fixes it
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._run_sensors", return_value=[("lint.sh", "still bad")]), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 1
        assert driver.run.call_count == SENSOR_RETRY_LIMIT  # every corrective retry (worker call now goes through run_subagent)
        mock_commit.assert_not_called()
        assert fake_sandbox.handle is not None
        assert fake_sandbox.handle._keep is True
        assert any(
            "agent/fake-branch" in line and "0 completed task(s)" in line
            for line in caplog.messages
        )
        state_file = tmp_path / ".agent-last-run.json"
        assert state_file.exists()
        assert json.loads(state_file.read_text())["branch"] == "agent/fake-branch"

    def test_second_task_sensor_exhaustion_preserves_first_tasks_commit(
        self, tmp_path, monkeypatch, caplog
    ):
        """Two-task plan: task 1 completes, task 2's sensors fail every retry —
        the branch is preserved and the recovery message names 1 completed task."""
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        sensors_call_count = 0

        def sensors_side_effect(cwd):
            nonlocal sensors_call_count
            sensors_call_count += 1
            if sensors_call_count == 1:
                return []  # task 1 passes on the first try
            return [("lint.sh", "still bad")]  # task 2 fails every check

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._run_sensors", side_effect=sensors_side_effect), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 1
        mock_commit.assert_called_once()  # task 1's commit went through
        assert fake_sandbox.handle is not None
        assert fake_sandbox.handle._keep is True
        assert any(
            "agent/fake-branch" in line and "1 completed task(s)" in line
            for line in caplog.messages
        )


# ── Code-health wiring ────────────────────────────────────────────────────────

class TestRunLoopCodeHealth:
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

    def test_clean_on_first_check_commits_without_finding_message(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop.check_code_health", return_value=[]), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 0
        mock_commit.assert_called_once()
        assert "[code-health]" not in caplog.text

    def test_findings_fixed_after_one_corrective_call_commits(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        driver.run.return_value = _ok()
        gate = MagicMock()
        gate.request.return_value = True

        findings = ["foo.py: bar (lines 1-60) — NLOC 60 exceeds threshold 50"]

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop.check_code_health", side_effect=[findings, []]), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 0
        mock_commit.assert_called_once()
        # one code-health corrective call (worker call now goes through run_subagent)
        assert driver.run.call_count == 1

    def test_findings_persist_through_budget_still_commit_and_surface_at_merge(
        self, tmp_path, monkeypatch, caplog
    ):
        """Code-health findings that survive every corrective retry do NOT fail
        closed (unlike sensors) — the commit proceeds and the remaining findings
        are recorded per-task for the merge-time [code-health] block."""
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        driver.run.return_value = _ok()
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        findings = ["foo.py: bar (lines 1-60) — NLOC 60 exceeds threshold 50"]

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop.check_code_health", return_value=findings), \
             patch("runner.loop._commit_task") as mock_commit, \
             patch("runner.loop._offer_merge", return_value="merged") as mock_offer, \
             patch("runner.loop._update_coverage_baseline"):
            code = run_loop("task")

        assert code == 0
        mock_commit.assert_called_once()
        mock_offer.assert_called_once()
        _, kwargs = mock_offer.call_args
        assert kwargs["code_health_issues"] == {1: findings}
        assert any(
            "[code-health]" in line and f"attempt {CODE_HEALTH_RETRY_LIMIT}/{CODE_HEALTH_RETRY_LIMIT}" in line
            for line in caplog.messages
        )


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
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 0
        assert driver.run_subagent.call_count == 3  # planner + worker + one review call
        driver.run.assert_not_called()  # no corrective
        mock_commit.assert_called_once()

    def test_review_requests_changes_once_then_approves(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        review_calls = 0

        def run_subagent_side_effect(agent_name, prompt, *args, **kwargs):
            nonlocal review_calls
            if agent_name == REVIEWER_AGENT:
                review_calls += 1
                if review_calls == 1:
                    return _ok(f"{REVIEW_CHANGES_SIGNAL}\nRename `foo` to `bar` in baz.py:12.")
                return _ok(f"{REVIEW_APPROVED_SIGNAL}\nfoo was renamed to bar as requested.")
            if agent_name == WORKER_AGENT:
                return self._worker_ok(tmp_path)(prompt, *args, **kwargs)
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.return_value = _ok()
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 0
        assert review_calls == 2
        assert driver.run.call_count == 1  # one review-corrective call (worker call now goes through run_subagent)
        mock_commit.assert_called_once()

    def test_review_requests_changes_every_retry_does_not_fail_closed(
        self, tmp_path, monkeypatch, caplog
    ):
        self._setup(tmp_path, monkeypatch)

        def run_subagent_side_effect(agent_name, prompt, *args, **kwargs):
            if agent_name == REVIEWER_AGENT:
                return _ok(f"{REVIEW_CHANGES_SIGNAL}\nStill not right.")
            if agent_name == WORKER_AGENT:
                return self._worker_ok(tmp_path)(prompt, *args, **kwargs)
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.return_value = _ok()
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        out = caplog.text
        # unlike a sensor failure, exhausting the review retry budget never
        # fails closed — the task still commits with the critique recorded
        assert code == 0
        assert driver.run.call_count == REVIEW_RETRY_LIMIT  # every review-corrective (worker call now goes through run_subagent)
        mock_commit.assert_called_once()
        assert "[review]" in out
        assert "budget exhausted" in out  # outstanding critique recorded, not discarded

    def test_sensor_regression_after_review_corrective_still_fails_closed(
        self, tmp_path, monkeypatch, caplog
    ):
        self._setup(tmp_path, monkeypatch)

        def run_subagent_side_effect(agent_name, prompt, *args, **kwargs):
            if agent_name == REVIEWER_AGENT:
                return _ok(f"{REVIEW_CHANGES_SIGNAL}\nFix the thing.")
            if agent_name == WORKER_AGENT:
                return self._worker_ok(tmp_path)(prompt, *args, **kwargs)
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.return_value = _ok()
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        sensors_call_count = 0

        def sensors_side_effect(cwd):
            nonlocal sensors_call_count
            sensors_call_count += 1
            if sensors_call_count == 1:
                return []  # passes before the review cycle starts
            return [("lint.sh", "regression")]  # fails on every post-corrective recheck

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._run_sensors", side_effect=sensors_side_effect), \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 1
        mock_commit.assert_not_called()
        assert fake_sandbox.handle is not None
        assert fake_sandbox.handle._keep is True
        assert any(
            "agent/fake-branch" in line and "0 completed task(s)" in line
            for line in caplog.messages
        )

    def test_sensor_retries_accumulate_across_initial_and_review_corrective_calls(
        self, tmp_path, monkeypatch
    ):
        """
        Sensor retries are tracked across two call sites: the post-worker
        _run_sensors_with_retry call and the review-corrective-loop call.
        This drives one fail-then-pass cycle through each and confirms both
        sites are actually exercised (all four side-effect values consumed)
        and the task still reaches a successful, approved commit.
        """
        self._setup(tmp_path, monkeypatch)

        review_calls = 0

        def run_subagent_side_effect(agent_name, prompt, *args, **kwargs):
            nonlocal review_calls
            if agent_name == REVIEWER_AGENT:
                review_calls += 1
                if review_calls == 1:
                    return _ok(f"{REVIEW_CHANGES_SIGNAL}\nStill needs a fix.")
                return _ok(f"{REVIEW_APPROVED_SIGNAL}\nAll good now.")
            if agent_name == WORKER_AGENT:
                return self._worker_ok(tmp_path)(prompt, *args, **kwargs)
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.return_value = _ok()
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
             ) as mock_sensors, \
             patch("runner.loop._commit_task") as mock_commit:
            code = run_loop("task")

        assert code == 0
        assert mock_sensors.call_count == 4  # both retry sites consumed all four side effects
        assert review_calls == 2
        mock_commit.assert_called_once()


# ── Run-level metrics summary ────────────────────────────────────────────────

class TestRunLoopMetricsSummary:
    def test_run_total_printed_and_appended_to_status(self, tmp_path, monkeypatch, caplog):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        def run_subagent_side_effect(agent_name, prompt, *args, **kwargs):
            if agent_name == REVIEWER_AGENT:
                return _ok(REVIEW_APPROVED_SIGNAL, session_id="sess-review")
            if agent_name == WORKER_AGENT:
                return worker_side_effect(prompt, *args, **kwargs)
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.return_value = _ok()
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch(
                 "runner.loop._run_sensors",
                 side_effect=[[], [("lint.sh", "bad")], []],
             ), \
             patch("runner.loop._commit_task"):
            code = run_loop("task")

        assert code == 0
        # run total = planner (1 call)
        #   + task 1 (worker + review approval, no sensor retry = 2 calls)
        #   + task 2 (worker + corrective + review approval, one sensor retry = 3 calls)
        # session id asserted as a real value (not None) so this fails if session
        # tracking gets wired to the wrong source.
        assert "[metrics] Task 1/2: 2 driver call(s), $0.0000, session sess-review" in caplog.messages
        assert "[metrics] Task 2/2: 3 driver call(s), $0.0000, session sess-review" in caplog.messages
        assert "[metrics] Run total: 6 driver call(s), $0.0000, session sess-review" in caplog.messages

        status_text = (tmp_path / "memory" / "status.md").read_text()
        assert "**Run metrics:** 6 driver call(s), $0.0000, session sess-review" in status_text

    def test_run_metrics_leave_no_dirty_status_md(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        _make_project(tmp_path)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        subprocess.run(["git", "commit", "-m", "scaffold project"], cwd=tmp_path,
                       check=True, capture_output=True, env=env)

        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        def run_subagent_side_effect(agent_name, prompt, *args, **kwargs):
            if agent_name == REVIEWER_AGENT:
                return _ok(REVIEW_APPROVED_SIGNAL, session_id="sess-review")
            if agent_name == WORKER_AGENT:
                return worker_side_effect(prompt, *args, **kwargs)
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task"):
            code = run_loop("task")

        assert code == 0
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "memory/status.md"],
            cwd=tmp_path, capture_output=True, text=True, check=False,
        ).stdout
        assert status == ""


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

    def test_no_leak_prints_no_warning(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._main_checkout_dirty_paths", return_value=[]):
            code = run_loop("task")

        assert code == 0
        assert "wrote outside its sandboxed worktree" not in caplog.text

    def test_leak_prints_warning_but_does_not_stop_the_loop(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
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
        out = caplog.text
        assert "wrote outside its sandboxed worktree" in out
        assert "runner/loop.py" in out

    def test_worker_failure_after_leak_still_prints_warning(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(lambda *a, **k: _fail())
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch(
                 "runner.loop._main_checkout_dirty_paths",
                 side_effect=[[], ["leaked.py"]],
             ):
            code = run_loop("task")

        assert code == 1
        out = caplog.text
        assert "wrote outside its sandboxed worktree" in out
        assert "leaked.py" in out

    def test_sensor_failure_after_leak_still_prints_warning(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)

        driver = MagicMock()
        driver.run_subagent.side_effect = _route_subagent(self._worker_ok(tmp_path))
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._run_sensors", return_value=[("lint.sh", "still bad")]), \
             patch(
                 "runner.loop._main_checkout_dirty_paths",
                 side_effect=[[], ["leaked.py"]],
             ):
            code = run_loop("task")

        assert code == 1
        out = caplog.text
        assert "wrote outside its sandboxed worktree" in out
        assert "leaked.py" in out


# ── Status.md check per task ──────────────────────────────────────────────────

class TestRunLoopStatusCheckPerTask:
    def test_warns_per_task_when_status_not_updated(self, tmp_path, monkeypatch, caplog):
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

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2  # one warning per task


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
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            run_loop("task")

        assert driver.run_subagent.call_count == 1
        gate.request.assert_called_once_with(PLAN_FILE)

    def test_valid_on_retry_reaches_gate(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan\n\n## Context\nno tasks yet.\n")

        planner_calls = {"n": 0}

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        def side_effect(agent_name, prompt, *args, **kwargs):
            if agent_name == REVIEWER_AGENT:
                return _ok(REVIEW_APPROVED_SIGNAL)
            if agent_name == WORKER_AGENT:
                return worker_side_effect(prompt, *args, **kwargs)
            planner_calls["n"] += 1
            if planner_calls["n"] == 1:
                return _ok("no signal here")
            (tmp_path / "plan.md").write_text(MINIMAL_PLAN)
            return _ok(PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = side_effect
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
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
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
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
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
            code = run_loop("task")

        assert driver.run_subagent.call_count == 2
        gate.request.assert_not_called()
        assert code == 1

    def test_retry_attempt_missing_plan_file_fails_closed(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text("# Plan\n\n## Context\nno tasks yet.\n")

        calls = {"n": 0}

        def side_effect(agent_name, prompt, *args, **kwargs):
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
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()):
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

    def test_commits_new_file(self, tmp_path, caplog):
        self._init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("hello")
        _commit_task(1, "Add greeting", tmp_path)
        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                             capture_output=True, text=True, check=False).stdout
        assert "Task 1: Add greeting" in log
        assert "committed" in caplog.text.lower()

    def test_nothing_to_commit_skips_silently(self, tmp_path, caplog):
        self._init_repo(tmp_path)
        _commit_task(1, "No changes", tmp_path)
        out = caplog.text
        assert "nothing" in out.lower()
        # still only the init commit
        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                             capture_output=True, text=True, check=False).stdout
        assert log.strip().count("\n") == 0  # single commit

    def test_commit_failure_logs_warning(self, tmp_path, caplog):
        self._init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("hello")

        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "commit"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="commit hook rejected")
            return real_run(cmd, *args, **kwargs)

        with patch("runner.loop.subprocess.run", side_effect=fake_run):
            _commit_task(1, "Add greeting", tmp_path)

        assert "commit failed" in caplog.text.lower()
        assert "commit hook rejected" in caplog.text


class TestCommitStatusUpdate:
    def _init_repo(self, path: Path) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"],
                       cwd=path, check=True, capture_output=True, env=env)

    def test_commits_status_file(self, tmp_path):
        self._init_repo(tmp_path)
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "status.md").write_text("update")
        _commit_status_update("test message", tmp_path)

        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                             capture_output=True, text=True, check=False).stdout
        assert "test message" in log

        status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                                capture_output=True, text=True, check=False).stdout
        assert "memory/status.md" not in status

        files = subprocess.run(
            ["git", "show", "HEAD", "--name-only", "--format="], cwd=tmp_path,
            capture_output=True, text=True, check=False,
        ).stdout
        assert [line for line in files.splitlines() if line] == ["memory/status.md"]

    def test_nothing_changed_does_not_crash(self, tmp_path, caplog):
        self._init_repo(tmp_path)
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "status.md").write_text("unchanged")
        subprocess.run(["git", "add", "memory/status.md"], cwd=tmp_path,
                       capture_output=True, check=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        subprocess.run(["git", "commit", "-m", "add status"], cwd=tmp_path,
                       capture_output=True, check=True, env=env)

        # status.md is already committed and unchanged, so there is nothing to commit.
        _commit_status_update("test message", tmp_path)

        out = caplog.text
        assert "[status] Commit failed" in out
        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                             capture_output=True, text=True, check=False).stdout
        assert "test message" not in log


class TestWriteLastRunState:
    def test_writes_correct_json_content(self, tmp_path):
        _init_repo(tmp_path)
        _write_last_run_state(tmp_path, "agent/fake-branch", tmp_path)

        state_file = tmp_path / ".agent-last-run.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert set(state.keys()) == {"branch", "tip_commit", "timestamp"}
        assert state["branch"] == "agent/fake-branch"

        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                               capture_output=True, text=True, check=False).stdout.strip()
        assert state["tip_commit"] == head

    def test_overwrites_previous_file(self, tmp_path):
        _init_repo(tmp_path)
        _write_last_run_state(tmp_path, "agent/first-branch", tmp_path)
        _write_last_run_state(tmp_path, "agent/second-branch", tmp_path)

        state = json.loads((tmp_path / ".agent-last-run.json").read_text())
        assert state["branch"] == "agent/second-branch"


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

    def test_diff_truncated_when_exceeding_max_lines(self, tmp_path, monkeypatch):
        self._init_repo(tmp_path)
        long_content = "\n".join(f"line {i}" for i in range(600)) + "\n"
        (tmp_path / "large.txt").write_text(long_content)

        import runner.loop as mod
        monkeypatch.setattr(mod, "MAX_DIFF_LINES", 10)

        diff = _task_diff(tmp_path)
        assert "large.txt" in diff
        assert "diff truncated:" in diff
        assert "additional lines omitted" in diff

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


class TestUpdateCoverageBaseline:
    def test_successful_run_writes_baseline_and_commits(self, tmp_path, monkeypatch):
        (tmp_path / "sensors").mkdir()
        (tmp_path / "sensors" / "test.sh").write_text("#!/bin/sh\n")

        def fake_run(cmd, cwd, **kwargs):
            if cmd[0] == "sh":
                (tmp_path / "coverage.json").write_text(
                    json.dumps({"totals": {"percent_covered": 87.654}})
                )
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        mock_run = MagicMock(side_effect=fake_run)
        monkeypatch.setattr(subprocess, "run", mock_run)

        _update_coverage_baseline(tmp_path)

        assert (tmp_path / ".coverage-baseline").read_text() == "87.654"
        assert not (tmp_path / "coverage.json").exists()
        commands = [call.args[0][:2] for call in mock_run.call_args_list]
        assert ["git", "add"] in commands
        assert ["git", "commit"] in commands

    def test_nonzero_exit_from_test_sh_does_not_block_baseline_update(self, tmp_path, monkeypatch):
        (tmp_path / "sensors").mkdir()
        (tmp_path / "sensors" / "test.sh").write_text("#!/bin/sh\n")
        (tmp_path / ".coverage-baseline").write_text("42.0")

        def fake_run(cmd, cwd, **kwargs):
            if cmd[0] == "sh":
                (tmp_path / "coverage.json").write_text(
                    json.dumps({"totals": {"percent_covered": 55.0}})
                )
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        mock_run = MagicMock(side_effect=fake_run)
        monkeypatch.setattr(subprocess, "run", mock_run)

        _update_coverage_baseline(tmp_path)

        assert (tmp_path / ".coverage-baseline").read_text() == "55.0"
        commands = [call.args[0][:2] for call in mock_run.call_args_list]
        assert ["git", "add"] in commands
        assert ["git", "commit"] in commands

    def test_missing_coverage_json_after_success_behaves_like_failure(self, tmp_path, monkeypatch):
        (tmp_path / "sensors").mkdir()
        (tmp_path / "sensors" / "test.sh").write_text("#!/bin/sh\n")
        (tmp_path / ".coverage-baseline").write_text("42.0")

        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr(subprocess, "run", mock_run)

        _update_coverage_baseline(tmp_path)

        assert (tmp_path / ".coverage-baseline").read_text() == "42.0"
        assert all(call.args[0][0] != "git" for call in mock_run.call_args_list)

    def test_no_test_sh_skips_without_running_anything(self, tmp_path, monkeypatch):
        mock_run = MagicMock()
        monkeypatch.setattr(subprocess, "run", mock_run)

        _update_coverage_baseline(tmp_path)

        assert not (tmp_path / ".coverage-baseline").exists()
        mock_run.assert_not_called()

    def test_real_project_agnostic_test_sh_writes_baseline_and_commits(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "sensors").mkdir()
        test_sh = tmp_path / "sensors" / "test.sh"
        test_sh.write_text(
            "#!/bin/sh\necho '{\"totals\": {\"percent_covered\": 73.2}}' > coverage.json\n"
        )
        test_sh.chmod(0o755)

        _update_coverage_baseline(tmp_path)

        assert (tmp_path / ".coverage-baseline").read_text() == "73.2"
        assert not (tmp_path / "coverage.json").exists()

        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                              capture_output=True, text=True, check=False).stdout
        assert "Update coverage baseline" in log


class TestOfferMerge:
    def test_no_commits_prints_warning_and_returns(self, tmp_path, caplog, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-b", "agent/empty"],
                       cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)

        outcome = _offer_merge("agent/empty", tmp_path)
        out = caplog.text
        assert "no commits" in out.lower()
        assert outcome == "no commits"

    def test_merge_y_squashes_into_one_commit_and_deletes_branch(self, tmp_path, caplog, monkeypatch):
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

        out = caplog.text
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

    def test_merge_y_stamps_verified_into_staged_concept_files(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        _init_repo(tmp_path)
        concepts_dir = tmp_path / "memory" / "concepts"
        concepts_dir.mkdir(parents=True)

        subprocess.run(["git", "checkout", "-b", "agent/feat"],
                       cwd=tmp_path, check=True, capture_output=True)
        (concepts_dir / "widgets.md").write_text(
            "---\ntype: pattern\ntags: [widgets]\n---\n\n# Widgets\n"
        )
        (concepts_dir / "index.md").write_text("---\ntype: index\n---\n\n# Index\n")
        subprocess.run(["git", "add", "memory"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Task 1: add widgets concept"],
                       cwd=tmp_path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)

        with patch("builtins.input", return_value="y"):
            outcome = _offer_merge("agent/feat", tmp_path, task="add widgets concept")

        assert outcome == "merged"
        assert "verified:" in (concepts_dir / "widgets.md").read_text()
        assert "verified:" not in (concepts_dir / "index.md").read_text()

        # the stamp is part of the squash commit, not left as an unstaged change
        status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                                capture_output=True, text=True, check=False).stdout
        assert status.strip() == ""

    def test_merge_n_preserves_branch_and_prints_instructions(self, tmp_path, caplog, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        _init_repo(tmp_path)
        _make_branch_with_commit(tmp_path, "agent/feat", "add feature")

        with patch("builtins.input", return_value="n"):
            outcome = _offer_merge("agent/feat", tmp_path)

        out = caplog.text
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

    def test_review_critiques_printed_before_prompt(self, tmp_path, caplog, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        _init_repo(tmp_path)
        _make_branch_with_commit(tmp_path, "agent/feat", "add feature")

        critiques = {1: "Missing error handling on the new endpoint."}

        with patch("builtins.input", return_value="n"):
            _offer_merge("agent/feat", tmp_path, critiques=critiques)

        out = caplog.text
        assert "[review]" in out
        assert "Task 1:" in out
        assert "Missing error handling on the new endpoint." in out
        # cleanup
        subprocess.run(
            ["git", "branch", "-D", "agent/feat"], cwd=tmp_path, capture_output=True, check=False
        )

    def test_code_health_issues_printed_before_prompt(self, tmp_path, caplog, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        _init_repo(tmp_path)
        _make_branch_with_commit(tmp_path, "agent/feat", "add feature")

        code_health_issues = {1: ["foo.py: bar (lines 1-60) — NLOC 60 exceeds threshold 50"]}

        with patch("builtins.input", return_value="n"):
            _offer_merge("agent/feat", tmp_path, code_health_issues=code_health_issues)

        out = caplog.text
        assert "[code-health]" in out
        assert "Task 1:" in out
        assert "NLOC 60 exceeds threshold 50" in out
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

    def test_noop_sandbox_skips_merge_offer(self, tmp_path, monkeypatch):
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

    def test_truthy_branch_passes_log_path_and_appends_returned_outcome(
        self, tmp_path, monkeypatch
    ):
        """
        Covers the run_loop() handoff at the handle.branch check: _offer_merge
        must be called with the run's log file path, and the returned outcome
        must be appended to that same file.
        """
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(SINGLE_TASK_PLAN)

        driver = MagicMock()

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run_subagent.side_effect = _route_subagent(worker_side_effect)
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task"), \
             patch("runner.loop._offer_merge", return_value="merged") as mock_offer, \
             patch("runner.loop._update_coverage_baseline") as mock_baseline:
            code = run_loop("task")

        assert code == 0
        mock_offer.assert_called_once()
        args, kwargs = mock_offer.call_args
        assert args[0] == "agent/fake-branch"
        log_path = kwargs["narrative_path"]
        assert log_path.parent == tmp_path / "logs"
        assert log_path.name.startswith("run-") and log_path.name.endswith(".log")
        assert log_path.exists()
        assert "\n## Outcome\nmerged\n" in log_path.read_text()
        mock_baseline.assert_called_once_with(Path.cwd().resolve())

    def test_declined_outcome_does_not_update_coverage_baseline(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(SINGLE_TASK_PLAN)

        driver = MagicMock()

        def worker_side_effect(prompt, context_files, cwd=None):
            status = tmp_path / "memory" / "status.md"
            status.write_text(status.read_text() + "- done\n")
            return _ok()

        driver.run_subagent.side_effect = _route_subagent(worker_side_effect)
        gate = MagicMock()
        gate.request.return_value = True
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task"), \
             patch("runner.loop._offer_merge", return_value="declined"), \
             patch("runner.loop._update_coverage_baseline") as mock_baseline:
            code = run_loop("task")

        assert code == 0
        mock_baseline.assert_not_called()


class TestPerformSquashMergeVerification:
    def test_committed_concept_file_gets_verified_stamp(self, tmp_path):
        import datetime as dt_module

        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        _init_repo(tmp_path)
        concepts_dir = tmp_path / "memory" / "concepts"
        concepts_dir.mkdir(parents=True)

        subprocess.run(["git", "checkout", "-b", "agent/feat"],
                       cwd=tmp_path, check=True, capture_output=True)
        (concepts_dir / "test-concept.md").write_text(
            "---\ntype: pattern\ntags: [test]\nsummary: a test concept\n---\n\n# Test Concept\n"
        )
        subprocess.run(["git", "add", "memory"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Task 1: add test concept"],
                       cwd=tmp_path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)

        outcome = _perform_squash_merge(
            "agent/feat", tmp_path, "add test concept", ["Task 1: add test concept"]
        )

        assert outcome == "merged"

        # committed content (working tree is clean post-commit, so this reflects HEAD)
        committed = subprocess.run(
            ["git", "show", "HEAD:memory/concepts/test-concept.md"],
            cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout
        status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                                capture_output=True, text=True, check=False).stdout
        assert status.strip() == ""

        today = dt_module.datetime.now(dt_module.UTC).date().isoformat()
        assert f'verified: [{{ by: "human", at: {today} }}]' in committed

    def test_branch_with_only_non_concept_files_skips_stamping(self, tmp_path):
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

        with patch("runner.loop._stamp_verified") as mock_stamp:
            outcome = _perform_squash_merge(
                "agent/feat", tmp_path, "add a", ["Task 1: add a"]
            )

        mock_stamp.assert_not_called()
        assert outcome == "merged"

    def test_squash_failure_preserves_branch_and_logs(self, tmp_path, caplog):
        _init_repo(tmp_path)
        _make_branch_with_commit(tmp_path, "agent/feat", "add feature")

        with patch(
            "runner.loop.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["git", "merge", "--squash", "agent/feat"], 1, stdout="", stderr="conflict"
            ),
        ):
            outcome = _perform_squash_merge(
                "agent/feat", tmp_path, "add feature", ["add feature"]
            )

        assert outcome == "squash failed"
        assert "squash failed" in caplog.text.lower()
        assert "preserved" in caplog.text.lower()

    def test_commit_failure_preserves_branch_and_logs(self, tmp_path, caplog):
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

        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "commit"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="commit hook rejected")
            return real_run(cmd, *args, **kwargs)

        with patch("runner.loop.subprocess.run", side_effect=fake_run):
            outcome = _perform_squash_merge(
                "agent/feat", tmp_path, "add a", ["Task 1: add a"]
            )

        assert outcome == "commit failed"
        assert "commit failed" in caplog.text.lower()
        assert "commit hook rejected" in caplog.text
        assert "preserved" in caplog.text.lower()
        branches = subprocess.run(["git", "branch"], cwd=tmp_path,
                                  capture_output=True, text=True, check=False).stdout
        assert "agent/feat" in branches


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

    def test_code_health_issues_prepended_to_diff_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ZELLIJ", "0")
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-b", "agent/feat"],
                       cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "a.txt").write_text("a")
        subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add a"],
                       cwd=tmp_path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True)

        code_health_issues = {1: ["foo.py: bar (lines 1-60) — NLOC 60 exceeds threshold 50"]}

        with patch("runner.loop._zellij_edit") as mock_edit:
            _show_diff_in_editor(
                "agent/feat", tmp_path, code_health_issues=code_health_issues
            )

        mock_edit.assert_called_once()
        diff_path = Path(mock_edit.call_args[0][0])
        content = diff_path.read_text()
        assert "# Outstanding code-health findings" in content
        assert "## Task 1" in content
        assert "NLOC 60 exceeds threshold 50" in content


class TestStampVerified:
    def test_noop_when_no_frontmatter(self, tmp_path):
        path = tmp_path / "concept.md"
        path.write_text("# No frontmatter here\n")

        _stamp_verified(path)

        assert path.read_text() == "# No frontmatter here\n"

    def test_noop_when_frontmatter_unterminated(self, tmp_path):
        path = tmp_path / "concept.md"
        path.write_text("---\ntype: pattern\nno closing delimiter\n")

        _stamp_verified(path)

        assert path.read_text() == "---\ntype: pattern\nno closing delimiter\n"

    def test_adds_verified_key_when_absent(self, tmp_path, monkeypatch):
        import datetime as dt_module

        class _FixedDatetime(dt_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt_module.datetime(2026, 8, 12, tzinfo=tz)

        monkeypatch.setattr("runner.loop.datetime", _FixedDatetime)

        path = tmp_path / "concept.md"
        path.write_text("---\ntype: pattern\ntags: [foo]\n---\n\n# Body\n")

        _stamp_verified(path)

        content = path.read_text()
        assert 'verified: [{ by: "human", at: 2026-08-12 }]' in content
        assert content.endswith("---\n\n# Body\n")

    def test_appends_to_existing_verified_list(self, tmp_path, monkeypatch):
        import datetime as dt_module

        class _FixedDatetime(dt_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt_module.datetime(2026, 8, 12, tzinfo=tz)

        monkeypatch.setattr("runner.loop.datetime", _FixedDatetime)

        path = tmp_path / "concept.md"
        path.write_text(
            '---\ntype: pattern\nverified: [{ by: "human", at: 2026-01-01 }]\n---\n\n# Body\n'
        )

        _stamp_verified(path)

        content = path.read_text()
        assert (
            'verified: [{ by: "human", at: 2026-01-01 }, { by: "human", at: 2026-08-12 }]'
            in content
        )

    def test_replaces_non_list_verified_value(self, tmp_path, monkeypatch):
        import datetime as dt_module

        class _FixedDatetime(dt_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt_module.datetime(2026, 8, 12, tzinfo=tz)

        monkeypatch.setattr("runner.loop.datetime", _FixedDatetime)

        path = tmp_path / "concept.md"
        path.write_text("---\ntype: pattern\nverified: true\n---\n\n# Body\n")

        _stamp_verified(path)

        content = path.read_text()
        assert 'verified: [{ by: "human", at: 2026-08-12 }]' in content

    def test_verified_entry_has_by_human_and_todays_date(self, tmp_path):
        import datetime as dt_module

        path = tmp_path / "concept.md"
        path.write_text("---\ntype: pattern\n---\n\n# Body\n")

        _stamp_verified(path)

        content = path.read_text()
        today = dt_module.datetime.now(dt_module.UTC).date().isoformat()
        assert f'verified: [{{ by: "human", at: {today} }}]' in content


# ── memory/concepts/index.md consistency ───────────────────────────────────────

class TestConceptIndex:
    def test_index_lists_every_concept_file(self):
        repo_root = Path(__file__).resolve().parent.parent
        concepts_dir = repo_root / "memory" / "concepts"
        index_text = (concepts_dir / "index.md").read_text()

        concept_files = sorted(
            p.name for p in concepts_dir.glob("*.md") if p.name != "index.md"
        )
        assert concept_files, "expected at least one concept file to check the index against"

        missing = [name for name in concept_files if name not in index_text]
        assert not missing, f"memory/concepts/index.md is missing links for: {missing}"
