"""Tests for InteractiveGate."""

import pytest
from unittest.mock import patch, call

from runner.gates.interactive import InteractiveGate


def make_plan(tmp_path, content="# Plan\n\nDo the thing."):
    p = tmp_path / "plan.md"
    p.write_text(content)
    return str(p)


class TestInteractiveGateApprove:
    CASES = [
        pytest.param("y", True, id="y_approves"),
        pytest.param("n", False, id="n_rejects"),
        pytest.param("Y", True, id="uppercase_Y_approves"),
        pytest.param("N", False, id="uppercase_N_rejects"),
    ]

    @pytest.mark.parametrize("answer,expected", CASES)
    def test_direct_answer(self, tmp_path, answer, expected):
        plan = make_plan(tmp_path)
        with patch("builtins.input", return_value=answer), \
             patch("builtins.print"):
            assert InteractiveGate().request(plan) is expected


class TestInteractiveGateUnknownInput:
    def test_reprompts_on_unknown_then_accepts_y(self, tmp_path):
        plan = make_plan(tmp_path)
        responses = iter(["x", "?", "y"])
        with patch("builtins.input", side_effect=responses), \
             patch("builtins.print"):
            assert InteractiveGate().request(plan) is True

    def test_reprompts_on_unknown_then_accepts_n(self, tmp_path):
        plan = make_plan(tmp_path)
        responses = iter(["nope", "n"])
        with patch("builtins.input", side_effect=responses), \
             patch("builtins.print"):
            assert InteractiveGate().request(plan) is False


class TestInteractiveGateFeedback:
    def test_feedback_appended_then_approved(self, tmp_path):
        plan = make_plan(tmp_path)
        # f → two lines of feedback → blank → y
        responses = iter(["f", "use table-driven tests", "add null cases", "", "y"])
        with patch("builtins.input", side_effect=responses), \
             patch("builtins.print"):
            result = InteractiveGate().request(plan)

        assert result is True
        content = (tmp_path / "plan.md").read_text()
        assert "## Human Feedback" in content
        assert "use table-driven tests" in content
        assert "add null cases" in content

    def test_feedback_appended_then_rejected(self, tmp_path):
        plan = make_plan(tmp_path)
        responses = iter(["f", "some note", "", "n"])
        with patch("builtins.input", side_effect=responses), \
             patch("builtins.print"):
            result = InteractiveGate().request(plan)

        assert result is False

    def test_empty_feedback_does_not_duplicate_section(self, tmp_path):
        plan = make_plan(tmp_path)
        # f → blank immediately (no feedback lines) → y
        responses = iter(["f", "", "y"])
        with patch("builtins.input", side_effect=responses), \
             patch("builtins.print"):
            InteractiveGate().request(plan)

        content = (tmp_path / "plan.md").read_text()
        assert content.count("## Human Feedback") == 0

    def test_second_feedback_appends_to_existing_section(self, tmp_path):
        plan = make_plan(tmp_path)
        # f → first note → blank → f → second note → blank → y
        responses = iter(["f", "first note", "", "f", "second note", "", "y"])
        with patch("builtins.input", side_effect=responses), \
             patch("builtins.print"):
            InteractiveGate().request(plan)

        content = (tmp_path / "plan.md").read_text()
        assert content.count("## Human Feedback") == 1
        assert "first note" in content
        assert "second note" in content


class TestInteractiveGateMissingPlan:
    def test_returns_false_when_plan_missing(self, tmp_path):
        with patch("builtins.print"):
            result = InteractiveGate().request(str(tmp_path / "nonexistent.md"))
        assert result is False
