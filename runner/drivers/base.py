from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentResult:
    text: str
    exit_code: int
    cost_usd: float | None = None
    session_id: str | None = None
    input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


class AgentDriver(ABC):
    """
    Thin interface over a model CLI or SDK.
    The loop only talks to this — concrete implementations are behind it.
    Swap tools by changing AGENT_TOOL env var; no loop code changes.
    """

    @abstractmethod
    def run(
        self,
        prompt: str,
        context_files: list[str] | None = None,
        cwd: Path | None = None,
        model: str | None = None,
    ) -> AgentResult:
        """
        Send a prompt, return the result. cwd sets the working directory for the agent process.
        When model is set, the call runs on that model; None keeps the tool's default.
        """
        ...

    @abstractmethod
    def run_subagent(
        self,
        agent_name: str,
        prompt: str,
        context_files: list[str] | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
        """
        Invoke a named sub-agent definition (from agents/<name>.md).
        The driver is responsible for mapping agent_name to the tool's
        sub-agent mechanism. cwd sets the working directory for the agent process.
        """
        ...

    def agent_model(self, agent_name: str) -> str | None:
        """
        Return the model an agent definition pins, so plain run() calls on behalf
        of that agent can use the same model. None when no model is pinned.
        """
        return None
