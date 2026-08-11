"""
metrics.py — driver call/cost/session accounting shared across the loop.
"""

from dataclasses import dataclass
from pathlib import Path

from .drivers.base import AgentDriver, AgentResult


@dataclass
class Metrics:
    calls: int = 0
    cost_usd: float = 0.0
    last_session_id: str | None = None

    def record(self, result: AgentResult) -> None:
        self.calls += 1
        if result.cost_usd is not None:
            self.cost_usd += result.cost_usd
        if result.session_id is not None:
            self.last_session_id = result.session_id


class _MeteredDriver(AgentDriver):
    """Wraps an AgentDriver, recording every call into a shared Metrics instance."""

    def __init__(self, inner: AgentDriver, metrics: Metrics) -> None:
        self._inner = inner
        self._metrics = metrics

    def run(
        self,
        prompt: str,
        context_files: list[str] | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
        result = self._inner.run(prompt, context_files=context_files, cwd=cwd)
        self._metrics.record(result)
        return result

    def run_subagent(
        self,
        agent_name: str,
        prompt: str,
        context_files: list[str] | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
        result = self._inner.run_subagent(
            agent_name, prompt, context_files=context_files, cwd=cwd
        )
        self._metrics.record(result)
        return result
