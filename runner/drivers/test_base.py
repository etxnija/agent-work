"""Tests for the AgentDriver ABC's non-abstract defaults."""

from runner.drivers.base import AgentDriver, AgentResult


class _MinimalDriver(AgentDriver):
    """Implements only the abstract methods, as a future driver might."""

    def run(self, prompt, context_files=None, cwd=None, model=None) -> AgentResult:
        return AgentResult(text="", exit_code=0)

    def run_subagent(self, agent_name, prompt, context_files=None, cwd=None) -> AgentResult:
        return AgentResult(text="", exit_code=0)


class TestAgentDriverDefaults:
    def test_agent_model_defaults_to_none(self):
        assert _MinimalDriver().agent_model("worker") is None
