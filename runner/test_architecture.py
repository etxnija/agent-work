from pathlib import Path
from unittest.mock import MagicMock, patch

from runner.architecture import (
    ARCH_CONVERGED_SIGNAL,
    ARCH_REVISED_SIGNAL,
    _architecture_verdict,
    run_architecture_review,
)
from runner.drivers.base import AgentResult


def _make_project(tmp_path: Path) -> Path:
    (tmp_path / "memory").mkdir()
    (tmp_path / "AGENTS.md").write_text("# AGENTS")
    (tmp_path / "memory" / "status.md").write_text("# Status\n")
    return tmp_path


def _res(text: str) -> AgentResult:
    return AgentResult(text=text, exit_code=0)


class TestArchitectureVerdict:
    def test_converged_with_recommendation(self):
        text = f"Some analysis.\n{ARCH_CONVERGED_SIGNAL}\nrecommendation text"
        converged, recommendation = _architecture_verdict(text)
        assert converged is True
        assert recommendation == "recommendation text"

    def test_revised_with_new_claim(self):
        text = f"Some doubt.\n{ARCH_REVISED_SIGNAL}\nnew claim"
        converged, claim = _architecture_verdict(text)
        assert converged is False
        assert claim == "new claim"

    def test_neither_marker_present_falls_back_to_not_converged(self):
        converged, text = _architecture_verdict("  I'm not sure about this.  ")
        assert converged is False
        assert text == "I'm not sure about this."


class TestRunArchitectureReview:
    def test_converged_on_round_1(self, tmp_path):
        _make_project(tmp_path)
        driver = MagicMock()
        driver.run_subagent.side_effect = [
            _res("claim text"),
            _res("doubt text"),
            _res(f"{ARCH_CONVERGED_SIGNAL}\nfinal recommendation"),
        ]
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.architecture.get_driver", return_value=driver), \
             patch("runner.architecture.get_gate", return_value=gate), \
             patch("builtins.print"):
            code = run_architecture_review(tmp_path)

        assert driver.run_subagent.call_count == 3
        recommendation_file = tmp_path / "architecture-recommendation.md"
        assert recommendation_file.exists()
        assert "Review metrics:" in recommendation_file.read_text()
        gate.request.assert_called_once_with("architecture-recommendation.md")
        assert code == 0

    def test_revised_then_converged_on_round_2(self, tmp_path):
        _make_project(tmp_path)
        driver = MagicMock()
        driver.run_subagent.side_effect = [
            _res("claim text"),
            _res("doubt text"),
            _res(f"{ARCH_REVISED_SIGNAL}\nnew claim"),
            _res("doubt text 2"),
            _res(f"{ARCH_CONVERGED_SIGNAL}\nfinal"),
        ]
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.architecture.get_driver", return_value=driver), \
             patch("runner.architecture.get_gate", return_value=gate), \
             patch("builtins.print"):
            code = run_architecture_review(tmp_path)

        assert driver.run_subagent.call_count == 5
        assert (tmp_path / "architecture-recommendation.md").exists()
        gate.request.assert_called_once_with("architecture-recommendation.md")
        assert code == 0

    def test_three_rounds_no_convergence(self, tmp_path):
        _make_project(tmp_path)
        driver = MagicMock()
        driver.run_subagent.side_effect = [
            _res("claim text"),
            _res("doubt text"),
            _res(f"{ARCH_REVISED_SIGNAL}\nclaim round 1"),
            _res("doubt text 2"),
            _res(f"{ARCH_REVISED_SIGNAL}\nclaim round 2"),
            _res("doubt text 3"),
            _res(f"{ARCH_REVISED_SIGNAL}\nclaim round 3"),
        ]
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.architecture.get_driver", return_value=driver), \
             patch("runner.architecture.get_gate", return_value=gate), \
             patch("builtins.print"):
            code = run_architecture_review(tmp_path)

        assert driver.run_subagent.call_count == 7
        recommendation_file = tmp_path / "architecture-recommendation.md"
        assert recommendation_file.exists()
        assert "No consensus" in recommendation_file.read_text()
        gate.request.assert_called_once_with("architecture-recommendation.md")
        assert code == 0

    def test_gate_declines_recommendation_left_for_later(self, tmp_path):
        _make_project(tmp_path)
        driver = MagicMock()
        driver.run_subagent.side_effect = [
            _res("claim text"),
            _res("doubt text"),
            _res(f"{ARCH_CONVERGED_SIGNAL}\nfinal recommendation"),
        ]
        gate = MagicMock()
        gate.request.return_value = False

        with patch("runner.architecture.get_driver", return_value=driver), \
             patch("runner.architecture.get_gate", return_value=gate), \
             patch("builtins.print"):
            code = run_architecture_review(tmp_path)

        gate.request.assert_called_once_with("architecture-recommendation.md")
        assert code == 0

    def test_no_hint_claim_prompt_has_no_hint_text_and_header_is_plain(self, tmp_path):
        _make_project(tmp_path)
        driver = MagicMock()
        driver.run_subagent.side_effect = [
            _res("claim text"),
            _res("doubt text"),
            _res(f"{ARCH_CONVERGED_SIGNAL}\nfinal recommendation"),
        ]
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.architecture.get_driver", return_value=driver), \
             patch("runner.architecture.get_gate", return_value=gate), \
             patch("builtins.print"):
            run_architecture_review(tmp_path)

        claim_prompt = driver.run_subagent.call_args_list[0].args[1]
        assert "starting point" not in claim_prompt
        assert "flagged" not in claim_prompt
        recommendation_file = tmp_path / "architecture-recommendation.md"
        assert recommendation_file.read_text().splitlines()[0] == "# Architecture Review"

    def test_hint_appears_in_claim_only_and_header_includes_hint(self, tmp_path):
        _make_project(tmp_path)
        driver = MagicMock()
        driver.run_subagent.side_effect = [
            _res("claim text"),
            _res("doubt text"),
            _res(f"{ARCH_CONVERGED_SIGNAL}\nfinal recommendation"),
        ]
        gate = MagicMock()
        gate.request.return_value = True

        with patch("runner.architecture.get_driver", return_value=driver), \
             patch("runner.architecture.get_gate", return_value=gate), \
             patch("builtins.print"):
            run_architecture_review(tmp_path, hint="loop.py is too big")

        claim_prompt = driver.run_subagent.call_args_list[0].args[1]
        doubt_prompt = driver.run_subagent.call_args_list[1].args[1]
        reconcile_prompt = driver.run_subagent.call_args_list[2].args[1]
        assert "loop.py is too big" in claim_prompt
        assert "loop.py is too big" not in doubt_prompt
        assert "loop.py is too big" not in reconcile_prompt
        recommendation_file = tmp_path / "architecture-recommendation.md"
        assert "(hint: loop.py is too big)" in recommendation_file.read_text().splitlines()[0]
