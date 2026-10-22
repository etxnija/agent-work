"""
metrics.py — driver call/cost/session accounting shared across the loop.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .drivers.base import AgentDriver, AgentResult

logger = logging.getLogger("agent_loop")

# Label for plain AgentDriver.run() calls, which are only used for corrective retries.
CORRECTIVE_LABEL = "corrective"


def _cache_read_share(cache_read: int, cache_write: int, uncached: int) -> float | None:
    """cache_read / (cache_read + cache_write + uncached); None when there is no input data."""
    total = cache_read + cache_write + uncached
    return cache_read / total if total else None


@dataclass
class LabelMetrics:
    """Accumulated usage for one call label (sub-agent name or "corrective")."""
    calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cache_read_share(self) -> float | None:
        return _cache_read_share(self.cache_read_tokens, self.cache_write_tokens, self.input_tokens)

    def record(self, result: AgentResult) -> None:
        self.calls += 1
        if result.cost_usd is not None:
            self.cost_usd += result.cost_usd
        if result.input_tokens is not None:
            self.input_tokens += result.input_tokens
        if result.cache_read_tokens is not None:
            self.cache_read_tokens += result.cache_read_tokens
        if result.cache_write_tokens is not None:
            self.cache_write_tokens += result.cache_write_tokens


@dataclass
class Metrics:
    calls: int = 0
    cost_usd: float = 0.0
    last_session_id: str | None = None
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    by_label: dict[str, LabelMetrics] = field(default_factory=dict)

    def cache_read_share(self) -> float | None:
        """Fraction of input tokens served from cache; None when no token data was recorded."""
        return _cache_read_share(self.cache_read_tokens, self.cache_write_tokens, self.input_tokens)

    def record(self, result: AgentResult, label: str | None = None) -> None:
        self.calls += 1
        if result.cost_usd is not None:
            self.cost_usd += result.cost_usd
        if result.input_tokens is not None:
            self.input_tokens += result.input_tokens
        if result.cache_read_tokens is not None:
            self.cache_read_tokens += result.cache_read_tokens
        if result.cache_write_tokens is not None:
            self.cache_write_tokens += result.cache_write_tokens
        if result.session_id is not None:
            self.last_session_id = result.session_id
        if label is not None:
            self.by_label.setdefault(label, LabelMetrics()).record(result)


class _MeteredDriver(AgentDriver):
    """Wraps an AgentDriver, recording every call into a shared Metrics instance."""

    def __init__(self, inner: AgentDriver, metrics: Metrics) -> None:
        self._inner = inner
        self._metrics = metrics

    def _record(self, result: AgentResult, label: str) -> None:
        self._metrics.record(result, label)
        logger.debug(
            f"[metrics] {label}: input {result.input_tokens}, "
            f"cache read {result.cache_read_tokens}, cache write {result.cache_write_tokens}"
        )

    def run(
        self,
        prompt: str,
        context_files: list[str] | None = None,
        cwd: Path | None = None,
        model: str | None = None,
    ) -> AgentResult:
        result = self._inner.run(prompt, context_files=context_files, cwd=cwd, model=model)
        self._record(result, CORRECTIVE_LABEL)
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
        self._record(result, agent_name)
        return result

    def agent_model(self, agent_name: str) -> str | None:
        # Not a driver call — delegate without recording metrics.
        return self._inner.agent_model(agent_name)
