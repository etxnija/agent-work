from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentResult:
    text: str
    exit_code: int
    cost_usd: float | None = None
    session_id: str | None = None


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
    ) -> AgentResult:
        """Send a prompt, return the result. cwd sets the working directory for the agent process."""
        ...

    @abstractmethod
    def run_subagent(
        self,
        agent_name: str,
        prompt: str,
        cwd: Path | None = None,
    ) -> AgentResult:
        """
        Invoke a named sub-agent definition (from agents/<name>.md).
        The driver is responsible for mapping agent_name to the tool's
        sub-agent mechanism. cwd sets the working directory for the agent process.
        """
        ...
