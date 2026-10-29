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
    _clear_last_run_state_if_matches,
    _clear_stale_last_run_state,
    _commit_status_update,
    _commit_task,
    _finalize_run,
    _log_handlers,
    _main_checkout_dirty_paths,
    _offer_merge,
    _outstanding_findings_section,
    _parse_task_concepts,
    _parse_tasks,
    _perform_squash_merge,
    _plan_invalid_reason,
    _review_verdict,
    _run_code_health_with_retry,
    _run_metrics_entry,
    _run_review_with_retry,
    _run_sensors,
    _run_sensors_with_retry,
    _run_task_checks,
    _show_diff_in_editor,
    _squash_subject,
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
from runner.metrics import Metrics
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

        def worker_fixes_lint(prompt, context_files, cwd=None, model=None):
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

        def fix_currently_reported(prompt, context_files, cwd=None, model=None):
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


class TestRunTaskChecksWorkerModel:
    """Every corrective driver.run() call uses the model the Worker definition pins."""

    CASES: ClassVar[list] = [
        pytest.param(
            [[("lint.sh", "bad")], []], [[]], [REVIEW_APPROVED_SIGNAL],
            id="sensor_retry",
        ),
        pytest.param(
            [[]], [["foo.py: bar — NLOC 60 exceeds threshold 50"], []], [REVIEW_APPROVED_SIGNAL],
            id="code_health_retry",
        ),
        pytest.param(
            [[], []], [[]], [f"{REVIEW_CHANGES_SIGNAL}\nFix it.", REVIEW_APPROVED_SIGNAL],
            id="review_correction",
        ),
    ]

    @pytest.mark.parametrize("sensor_results,health_results,review_texts", CASES)
    def test_corrective_call_uses_worker_model(
        self, tmp_path, sensor_results, health_results, review_texts
    ):
        driver = MagicMock()
        driver.agent_model.return_value = "claude-sonnet-5-5"
        driver.run.return_value = _ok()
        driver.run_subagent.side_effect = [_ok(text) for text in review_texts]
        handle = MagicMock(path=tmp_path)

        with patch("runner.loop._run_sensors", side_effect=sensor_results), \
             patch("runner.loop.check_code_health", side_effect=health_results), \
             patch("runner.loop._task_diff", return_value=""):
            findings, failures = _run_task_checks(
                driver, handle, 1, 1, "task text",
                "plan.md", "AGENTS.md", "memory/status.md", ["AGENTS.md"], {},
            )

        assert findings == []
        assert failures == []
        driver.agent_model.assert_called_once_with(WORKER_AGENT)
        assert driver.run.call_count == 1
        assert driver.run.call_args.kwargs["model"] == "claude-sonnet-5-5"


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

    def _run_with_state_for_branch(self, tmp_path, monkeypatch, branch, with_branch):
        _make_project(tmp_path)
        _init_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(SINGLE_TASK_PLAN)
        if with_branch:
            _make_branch_with_commit(tmp_path, branch, "work")
        (tmp_path / ".agent-last-run.json").write_text(json.dumps({"branch": branch}))

        gate = MagicMock()
        gate.request.return_value = False
        fake_sandbox = _FakeBranchSandbox(tmp_path, "agent/fake-branch")

        with patch("runner.loop.get_driver", return_value=MagicMock()), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=fake_sandbox), \
             patch("runner.loop._generate_plan", return_value=0):
            return run_loop("task")

    def test_stale_last_run_state_removed_when_branch_missing(self, tmp_path, monkeypatch, caplog):
        with caplog.at_level(logging.INFO, logger="agent_loop"):
            code = self._run_with_state_for_branch(tmp_path, monkeypatch, "agent/gone", False)

        assert code == 2
        assert not (tmp_path / ".agent-last-run.json").exists()
        assert any("[cleanup] Removed stale" in m for m in caplog.messages)

    def test_last_run_state_kept_when_branch_exists(self, tmp_path, monkeypatch):
        code = self._run_with_state_for_branch(tmp_path, monkeypatch, "agent/kept", True)

        assert code == 2
        state_file = tmp_path / ".agent-last-run.json"
        assert state_file.exists()
        assert json.loads(state_file.read_text())["branch"] == "agent/kept"


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
        # No token data from the fake driver: share renders as n/a, not 0%.
        assert (
            "[metrics] Run cache: read 0, write 0, uncached input 0 tokens, cache-read share n/a"
            in caplog.messages
        )
        assert "cache read 0 / write 0 / input 0 (share n/a)" in status_text

    def test_cache_usage_reported_in_log_and_status(self, tmp_path, monkeypatch, caplog):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(MINIMAL_PLAN)

        def tokens(read, write, input_tokens, session_id=None, text=""):
            return AgentResult(
                text=text, exit_code=0, session_id=session_id, input_tokens=input_tokens,
                cache_read_tokens=read, cache_write_tokens=write,
            )

        def run_subagent_side_effect(agent_name, prompt, *args, **kwargs):
            if agent_name == REVIEWER_AGENT:
                return tokens(600, 0, 0, "sess-review", REVIEW_APPROVED_SIGNAL)
            if agent_name == WORKER_AGENT:
                status = tmp_path / "memory" / "status.md"
                status.write_text(status.read_text() + "- done\n")
                return tokens(100, 200, 100)
            return tokens(0, 0, 0, text=PLAN_READY_SIGNAL)

        driver = MagicMock()
        driver.run_subagent.side_effect = run_subagent_side_effect
        driver.run.return_value = _ok()
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.loop.get_driver", return_value=driver), \
             patch("runner.loop.get_gate", return_value=gate), \
             patch("runner.loop.get_sandbox", return_value=NoopSandbox()), \
             patch("runner.loop._run_sensors", return_value=[]), \
             patch("runner.loop._commit_task"):
            code = run_loop("task")

        assert code == 0
        # 2 tasks: worker read/write/input = 100/200/100 each, reviewer 600/0/0 each.
        # Totals: read 1400, write 400, input 200 -> share 1400/2000 = 70%.
        assert (
            "[metrics] Run cache: read 1400, write 400, uncached input 200 tokens, "
            "cache-read share 70%"
        ) in caplog.messages
        label_lines = [m for m in caplog.messages if m.startswith("[metrics]   ")]
        assert label_lines == [
            "[metrics]   planner: 1 call(s), $0.0000, read 0, write 0, input 0, share n/a",
            "[metrics]   reviewer: 2 call(s), $0.0000, read 1200, write 0, input 0, share 100%",
            "[metrics]   worker: 2 call(s), $0.0000, read 200, write 400, input 200, share 25%",
        ]
        status_text = (tmp_path / "memory" / "status.md").read_text()
        assert (
            "**Run metrics:** 5 driver call(s), $0.0000, session sess-review"
            ", cache read 1400 / write 400 / input 200 (share 70%)"
        ) in status_text

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


def _make_branch_with_file_commit(repo: Path, branch: str, message: str) -> None:
    """Like _make_branch_with_commit, but stages a real file so the commit
    isn't empty — required for callers that then squash-merge the branch,
    since `git commit` fails with nothing staged."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(["git", "checkout", "-b", branch], cwd=repo, check=True, capture_output=True)
    (repo / "change.txt").write_text(message)
    subprocess.run(["git", "add", "change.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message],
                   cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "checkout", "-"], cwd=repo, check=True, capture_output=True)


def _last_commit_subject(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _last_commit_body(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


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

    def test_clear_deletes_file_when_branch_matches(self, tmp_path):
        _init_repo(tmp_path)
        _write_last_run_state(tmp_path, "agent/feat", tmp_path)

        _clear_last_run_state_if_matches(tmp_path, "agent/feat")

        assert not (tmp_path / ".agent-last-run.json").exists()

    def test_clear_keeps_file_for_different_branch(self, tmp_path):
        _init_repo(tmp_path)
        _write_last_run_state(tmp_path, "agent/other", tmp_path)

        _clear_last_run_state_if_matches(tmp_path, "agent/feat")

        state_file = tmp_path / ".agent-last-run.json"
        assert state_file.exists()
        assert json.loads(state_file.read_text())["branch"] == "agent/other"

    def test_clear_is_noop_when_file_missing(self, tmp_path):
        _clear_last_run_state_if_matches(tmp_path, "agent/feat")

        assert not (tmp_path / ".agent-last-run.json").exists()

    def test_clear_leaves_corrupt_file_in_place(self, tmp_path):
        state_file = tmp_path / ".agent-last-run.json"
        state_file.write_text("{not json")

        _clear_last_run_state_if_matches(tmp_path, "agent/feat")

        assert state_file.read_text() == "{not json"

    @pytest.mark.parametrize("content", ["[]", '"x"', "null", "42"])
    def test_clear_leaves_non_object_json_in_place(self, tmp_path, content):
        state_file = tmp_path / ".agent-last-run.json"
        state_file.write_text(content)

        _clear_last_run_state_if_matches(tmp_path, "agent/feat")

        assert state_file.read_text() == content

    @pytest.mark.parametrize(
        "content",
        [b"{not json", b"[]", b"null", b"{}", b'{"branch": ""}', b'{"branch": null}', b"\xff\xfe\x00bin"],
    )
    def test_stale_cleanup_leaves_unusable_state_in_place(self, tmp_path, content):
        _init_repo(tmp_path)
        state_file = tmp_path / ".agent-last-run.json"
        state_file.write_bytes(content)

        _clear_stale_last_run_state(tmp_path)

        assert state_file.read_bytes() == content

    def test_squash_merge_keeps_state_when_branch_delete_fails(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        _make_branch_with_file_commit(repo, "agent/feat", "Task 1: add file")
        # A branch checked out in another worktree cannot be deleted.
        subprocess.run(["git", "worktree", "add", str(tmp_path / "wt"), "agent/feat"],
                       cwd=repo, capture_output=True, check=True)
        _write_last_run_state(repo, "agent/feat", repo)

        outcome = _perform_squash_merge("agent/feat", repo, "add file", ["Task 1: add file"])

        assert outcome == "merged"
        assert (repo / ".agent-last-run.json").exists()

    def test_squash_merge_clears_state_for_deleted_branch(self, tmp_path):
        _init_repo(tmp_path)
        _make_branch_with_file_commit(tmp_path, "agent/feat", "Task 1: add file")
        _write_last_run_state(tmp_path, "agent/feat", tmp_path)

        outcome = _perform_squash_merge("agent/feat", tmp_path, "add file", ["Task 1: add file"])

        assert outcome == "merged"
        assert not (tmp_path / ".agent-last-run.json").exists()


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


class TestSquashSubject:
    def test_empty_task_falls_back_to_branch(self):
        assert _squash_subject("   ", "agent/test") == "agent/test"

    def test_short_first_line_used_verbatim(self):
        assert _squash_subject("Add the foo helper", "agent/test") == "Add the foo helper"

    def test_only_first_line_considered(self):
        task = "Short subject\nA much longer body line that would not fit otherwise."
        assert _squash_subject(task, "agent/test") == "Short subject"

    def test_long_first_line_truncated_at_word_boundary(self):
        first_line = "word " * 20  # 100 chars, well past the 72-char limit
        expected = "word word word word word word word word word word word word word word…"
        assert _squash_subject(first_line, "agent/test") == expected

    def test_long_first_line_without_spaces_hard_truncated(self):
        first_line = "x" * 100
        assert _squash_subject(first_line, "agent/test") == "x" * 72 + "…"


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

        def worker_side_effect(prompt, context_files, cwd=None, model=None):
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

    def test_short_task_becomes_exact_subject(self, tmp_path):
        _init_repo(tmp_path)
        _make_branch_with_file_commit(tmp_path, "agent/feat", "Task 1: add test concept")

        task = "add test concept"
        outcome = _perform_squash_merge(
            "agent/feat", tmp_path, task, ["Task 1: add test concept"]
        )

        assert outcome == "merged"
        assert _last_commit_subject(tmp_path) == task

    def test_long_multiline_task_truncated_subject(self, tmp_path):
        _init_repo(tmp_path)
        _make_branch_with_file_commit(tmp_path, "agent/feat", "Task 1: refactor module")

        task = (
            "Refactor the authentication module to split token validation "
            "from session management.\n\n"
            "This addresses a long-standing coupling issue where the two "
            "concerns were tangled together in a single class, making it "
            "hard to test either in isolation.\n\n"
            "Follow-up work will extend this to cover the OAuth callback "
            "path as well, once the core split lands."
        )
        assert len(task) >= 300
        assert task.count("\n") >= 3

        outcome = _perform_squash_merge(
            "agent/feat", tmp_path, task, ["Task 1: refactor module"]
        )

        assert outcome == "merged"
        subject = _last_commit_subject(tmp_path)
        assert len(subject) <= 73

        body = _last_commit_body(tmp_path)
        assert task in body

    def test_truncation_at_word_boundary(self, tmp_path):
        _init_repo(tmp_path)
        _make_branch_with_file_commit(tmp_path, "agent/feat", "Task 1: add words")

        task = ("word " * 20).strip()

        outcome = _perform_squash_merge(
            "agent/feat", tmp_path, task, ["Task 1: add words"]
        )

        assert outcome == "merged"
        subject = _last_commit_subject(tmp_path)
        assert subject.endswith("…")

        core = subject[:-1]
        assert len(core) < len(task)
        assert task[len(core)] == " "

    def test_empty_task_falls_back_to_branch(self, tmp_path):
        _init_repo(tmp_path)
        _make_branch_with_file_commit(tmp_path, "agent/feat", "Task 1: add a")

        outcome = _perform_squash_merge("agent/feat", tmp_path, "   ", ["Task 1: add a"])

        assert outcome == "merged"
        assert _last_commit_subject(tmp_path) == "agent/feat"


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


# ── Run metrics carried in the squash-merge commit ────────────────────────────

_GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout


def _metrics_repo(tmp_path: Path) -> Path:
    """Repo with a committed memory/status.md on the default branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "memory").mkdir()
    (repo / "memory" / "status.md").write_text("# Status\n")
    _git(repo, "add", "memory")
    _git(repo, "commit", "-m", "add status")
    return repo


def _branch_with_change(repo: Path, branch: str, status_line: str | None = None) -> None:
    _git(repo, "checkout", "-b", branch)
    (repo / "change.txt").write_text("change")
    _git(repo, "add", "change.txt")
    if status_line is not None:
        status = repo / "memory" / "status.md"
        status.write_text(status.read_text() + status_line)
        _git(repo, "add", "memory/status.md")
    _git(repo, "commit", "-m", "Task 1: change")
    _git(repo, "checkout", "-")


def _populated_metrics() -> Metrics:
    m = Metrics()
    m.calls = 3
    m.cost_usd = 1.5
    m.last_session_id = "sess-x"
    return m


def _run_finalize(
    repo: Path, tmp_path: Path, branch: str, answer: str, monkeypatch, cwd: Path | None = None,
    guard_warnings: list[str] | None = None,
) -> None:
    monkeypatch.chdir(cwd or repo)
    monkeypatch.delenv("ZELLIJ", raising=False)
    log_path = tmp_path / "run.log"
    log_path.write_text("log\n")
    with patch("builtins.input", return_value=answer), \
         patch("runner.loop._update_coverage_baseline"):
        _finalize_run(
            "task", repo, log_path, WorkspaceHandle(path=repo, branch=branch),
            {}, {}, _populated_metrics(), guard_warnings=guard_warnings,
        )


def _commit_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").strip())


def _status_text(repo: Path) -> str:
    return (repo / "memory" / "status.md").read_text()


class TestRunMetricsInSquashCommit:
    def test_merged_run_carries_metrics_in_squash_commit(self, tmp_path, monkeypatch):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")
        before = _commit_count(repo)

        _run_finalize(repo, tmp_path, "agent/feat", "y", monkeypatch)

        assert _commit_count(repo) == before + 1
        assert "**Run metrics:**" in _git(repo, "show", "HEAD", "--", "memory/status.md")
        assert "Record run metrics" not in _git(repo, "log", "--format=%s")
        assert _git(repo, "status", "--porcelain").strip() == ""

    def test_declined_run_uses_separate_metrics_commit(self, tmp_path, monkeypatch):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")

        _run_finalize(repo, tmp_path, "agent/feat", "n", monkeypatch)

        assert _last_commit_subject(repo) == "Record run metrics"
        assert _status_text(repo).count("**Run metrics:**") == 1

    def test_squash_failed_falls_back_to_separate_commit(self, tmp_path, monkeypatch):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")
        # An untracked file the squash would overwrite makes `git merge --squash` refuse.
        (repo / "change.txt").write_text("local")

        _run_finalize(repo, tmp_path, "agent/feat", "y", monkeypatch)

        assert _last_commit_subject(repo) == "Record run metrics"
        assert _status_text(repo).count("**Run metrics:**") == 1

    def test_no_commits_falls_back_to_separate_commit(self, tmp_path, monkeypatch):
        repo = _metrics_repo(tmp_path)
        _git(repo, "branch", "agent/empty")

        _run_finalize(repo, tmp_path, "agent/empty", "y", monkeypatch)

        assert _last_commit_subject(repo) == "Record run metrics"
        assert _status_text(repo).count("**Run metrics:**") == 1

    def test_branch_status_entry_precedes_metrics_once_each(self, tmp_path, monkeypatch):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat", status_line="\n- branch entry\n")

        _run_finalize(repo, tmp_path, "agent/feat", "y", monkeypatch)

        text = _status_text(repo)
        assert text.count("- branch entry") == 1
        assert text.count("**Run metrics:**") == 1
        assert text.index("- branch entry") < text.index("**Run metrics:**")
        assert "Record run metrics" not in _git(repo, "log", "--format=%s")

    def test_commit_failure_restores_status_and_staged_state(self, tmp_path):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")
        hook = repo / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        entry = _run_metrics_entry(_populated_metrics())

        outcome = _perform_squash_merge(
            "agent/feat", repo, "task", ["Task 1: change"], metrics_entry=entry
        )

        assert outcome == "commit failed"
        assert "**Run metrics:**" not in _status_text(repo)
        staged = _git(repo, "diff", "--cached", "--name-only").split()
        assert staged == ["change.txt"]

    def test_finalize_after_commit_failure_records_metrics_once(self, tmp_path, monkeypatch):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")
        # Reject the squash commit but let the fallback "Record run metrics" commit through.
        hook = repo / ".git" / "hooks" / "commit-msg"
        hook.write_text('#!/bin/sh\ngrep -q "Record run metrics" "$1" && exit 0\nexit 1\n')
        hook.chmod(0o755)

        _run_finalize(repo, tmp_path, "agent/feat", "y", monkeypatch)

        assert _status_text(repo).count("**Run metrics:**") == 1
        assert _last_commit_subject(repo) == "Record run metrics"
        # Fallback commit is scoped to status.md; the failed squash stays staged, not committed.
        assert _git(repo, "show", "HEAD", "--name-only", "--format=").split() == ["memory/status.md"]
        assert "change.txt" in _git(repo, "diff", "--cached", "--name-only")

    @pytest.mark.parametrize("answer", ["y", "n"])
    def test_status_path_independent_of_cwd(self, tmp_path, monkeypatch, answer):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "memory").mkdir(parents=True)
        (elsewhere / "memory" / "status.md").write_text("# Other\n")

        _run_finalize(repo, tmp_path, "agent/feat", answer, monkeypatch, cwd=elsewhere)

        assert _status_text(repo).count("**Run metrics:**") == 1
        assert "**Run metrics:**" not in (elsewhere / "memory" / "status.md").read_text()

    def test_squash_without_metrics_entry_leaves_status_untouched(self, tmp_path):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")

        outcome = _perform_squash_merge("agent/feat", repo, "task", ["Task 1: change"])

        assert outcome == "merged"
        assert _git(repo, "show", "HEAD", "--name-only", "--format=").split() == ["change.txt"]

    def test_run_metrics_entry_text(self):
        m = _populated_metrics()
        m.cache_read_tokens = 80
        m.cache_write_tokens = 10
        m.input_tokens = 10
        assert _run_metrics_entry(m) == (
            "\n**Run metrics:** 3 driver call(s), $1.5000, session sess-x"
            ", cache read 80 / write 10 / input 10 (share 80%)\n"
        )
        assert _run_metrics_entry(Metrics()).endswith("(share n/a)\n")


# ── Append-only status.md guard wiring ────────────────────────────────────────

class TestStatusGuardWiring:
    ORIGINAL = "- old entry\n- second entry\n"

    @pytest.mark.parametrize("separate_worktree", [False, True], ids=["main-checkout", "worktree-copy"])
    def test_rewritten_status_is_restored(self, tmp_path, separate_worktree):
        from runner.loop import STATUS_MD, _run_one_task

        root = tmp_path / "root"
        worktree = tmp_path / "wt" if separate_worktree else root
        for base in (root, worktree):
            (base / "memory").mkdir(parents=True, exist_ok=True)
            (base / STATUS_MD).write_text(self.ORIGINAL)
        status_abs = str(root / STATUS_MD)
        handle = WorkspaceHandle(path=worktree, branch="")
        target = worktree / STATUS_MD

        def rewrite(agent_name, prompt, context_files=None, cwd=None):
            target.write_text("- REWRITTEN\n- second entry\n- new entry\n")
            return _ok("done")

        driver = MagicMock()
        driver.run_subagent.side_effect = rewrite
        guard_warnings: list[str] = []

        with patch("runner.loop._run_task_checks", return_value=([], [])), \
             patch("runner.loop._commit_task"), \
             patch("runner.loop._main_checkout_dirty_paths", return_value=[]):
            _run_one_task(
                driver, handle, 1, 1, "1. **t** — x", "plan.md", "AGENTS.md", status_abs,
                root, {}, Metrics(), guard_warnings,
            )

        assert target.read_text() == self.ORIGINAL + "- REWRITTEN\n- new entry\n"
        expected = (
            "Task 1/1: the Worker modified existing memory/status.md content; "
            "restored the original and kept 2 new line(s)."
        )
        assert guard_warnings == [expected + (" (worktree copy)" if separate_worktree else "")]


# ── Outstanding findings in the squash commit body ───────────────────────────

def _full_message(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%B")


class TestOutstandingFindingsSection:
    @pytest.mark.parametrize("critiques,health,guard,expected", [
        (None, None, None, ""),
        ({}, {}, [], ""),
        ({2: "b", 1: "a"}, None, None, [
            "- Task 1 critique: a", "- Task 2 critique: b"]),
        ({1: "a"}, {3: ["x", "y"], 2: ["z"]}, ["g"], [
            "- Task 1 critique: a", "- Task 2 code-health: z",
            "- Task 3 code-health: x", "- Task 3 code-health: y", "- Guard: g"]),
        ({1: "  multi\n\n line\t text "}, None, None, ["- Task 1 critique: multi line text"]),
        (None, None, ["# heading\nlike"], ["- Guard: # heading like"]),
    ])
    def test_ordering_and_collapse(self, critiques, health, guard, expected):
        section = _outstanding_findings_section(critiques, health, guard)
        if expected == "":
            assert section == ""
        else:
            assert section.splitlines() == [
                "Outstanding review findings (not resolved before merge):", *expected]

    @pytest.mark.parametrize("length,capped", [(599, False), (600, False), (601, True), (2000, True)])
    def test_cap_applies_to_all_kinds(self, length, capped):
        text = "a" * length
        section = _outstanding_findings_section({1: text}, {1: [text]}, [text])
        for line in section.splitlines()[1:]:
            body = line.split(": ", 1)[1]
            assert len(body) == min(length, 600)
            assert body.endswith("…") == capped


class TestFindingsInSquashCommit:
    def test_findings_recorded_in_body(self, tmp_path):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")
        long = ("word\n" * 300)

        outcome = _perform_squash_merge(
            "agent/feat", repo, "task", ["Task 1: change"],
            critiques={1: long}, code_health_issues={1: ["f1", "f2"]}, guard_warnings=["careful"],
        )

        assert outcome == "merged"
        msg = _full_message(repo)
        assert msg.count("Outstanding review findings (not resolved before merge):") == 1
        item_lines = [l for l in msg.splitlines() if l.startswith(("- Task 1 c", "- Guard"))]
        assert len(item_lines) == 4
        critique_line = next(l for l in item_lines if "critique" in l)
        text = critique_line.split(": ", 1)[1]
        assert len(text) == 600 and text.endswith("…")
        assert "- Task 1 code-health: f1" in msg and "- Task 1 code-health: f2" in msg
        assert "- Guard: careful" in msg
        found = _git(repo, "log", "--grep", "Outstanding review findings", "--format=%H").split()
        assert found == [_git(repo, "rev-parse", "HEAD").strip()]

    @pytest.mark.parametrize("kwargs", [
        {},
        {"critiques": None, "code_health_issues": None, "guard_warnings": None},
        {"critiques": {}, "code_health_issues": {}, "guard_warnings": []},
    ])
    def test_empty_findings_leave_message_unchanged(self, tmp_path, kwargs):
        messages = []
        for name, kw in (("a", {}), ("b", kwargs)):
            (tmp_path / name).mkdir()
            repo = _metrics_repo(tmp_path / name)
            _branch_with_change(repo, "agent/feat")
            assert _perform_squash_merge("agent/feat", repo, "task", ["Task 1: change"], **kw) == "merged"
            messages.append(_full_message(repo))
        assert messages[0] == messages[1]
        assert messages[0] == "task\n\n- Task 1: change\n\ntask\n\n"
        assert "Outstanding review findings" not in messages[0]

    def test_metrics_and_findings_share_commit(self, tmp_path):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")
        entry = _run_metrics_entry(_populated_metrics())

        _perform_squash_merge(
            "agent/feat", repo, "task", ["Task 1: change"],
            metrics_entry=entry, critiques={1: "fix this"},
        )

        assert "**Run metrics:**" in _git(repo, "show", "HEAD", "--", "memory/status.md")
        assert "- Task 1 critique: fix this" in _full_message(repo)

    def test_offer_merge_passes_findings(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZELLIJ", raising=False)
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")

        with patch("builtins.input", return_value="y"):
            outcome = _offer_merge(
                "agent/feat", repo, "task",
                critiques={1: "needs work"}, code_health_issues={1: ["too long"]},
            )

        assert outcome == "merged"
        msg = _full_message(repo)
        assert "- Task 1 critique: needs work" in msg
        assert "- Task 1 code-health: too long" in msg


class TestGuardWarningsAtMerge:
    def test_merged_run_carries_guard_and_metrics_in_one_commit(self, tmp_path, monkeypatch):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")
        before = _commit_count(repo)

        _run_finalize(repo, tmp_path, "agent/feat", "y", monkeypatch, guard_warnings=["Task 1/1: restored"])

        assert _commit_count(repo) == before + 1
        assert "- Guard: Task 1/1: restored" in _full_message(repo)
        assert "**Run metrics:**" in _git(repo, "show", "HEAD", "--", "memory/status.md")

    def test_declined_run_records_no_guard_section(self, tmp_path, monkeypatch):
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")

        _run_finalize(repo, tmp_path, "agent/feat", "n", monkeypatch, guard_warnings=["Task 1/1: restored"])

        assert _last_commit_subject(repo) == "Record run metrics"
        assert _git(repo, "log", "--grep", "Outstanding review findings", "--format=%H").strip() == ""

    def test_offer_merge_logs_guard_block(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("ZELLIJ", raising=False)
        repo = _metrics_repo(tmp_path)
        _branch_with_change(repo, "agent/feat")

        with caplog.at_level("INFO"), patch("builtins.input", return_value="n"):
            _offer_merge("agent/feat", repo, "task", guard_warnings=["Task 1/1: restored"])

        assert "[guard] Outstanding memory/status.md guard warnings:" in caplog.text
        assert "  Task 1/1: restored" in caplog.text
