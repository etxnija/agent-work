import os

from .base import AgentDriver


def get_driver(name: str | None = None) -> AgentDriver:
    """
    Factory: returns the configured driver.
    Override with AGENT_TOOL env var or pass name explicitly.

    Supported values: "claude" (default)
    Planned: "gemini" — see status.md backlog
    """
    tool = name or os.getenv("AGENT_TOOL", "claude")
    match tool:
        case "claude":
            from .claude import ClaudeDriver
            return ClaudeDriver()
        case _:
            raise ValueError(
                f"Unknown AGENT_TOOL '{tool}'. "
                f"Supported: claude. See status.md for planned additions."
            )
