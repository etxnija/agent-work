import os
from .base import ApprovalGate


def get_gate(name: str | None = None) -> ApprovalGate:
    """
    Factory: returns the configured approval gate.
    Override with AGENT_MODE env var or pass name explicitly.

    Supported values: "interactive" (default)
    Planned: "file" — polls for plan.approved sentinel (Phase 4)
    See status.md backlog.
    """
    mode = name or os.getenv("AGENT_MODE", "interactive")
    match mode:
        case "interactive":
            from .interactive import InteractiveGate
            return InteractiveGate()
        case _:
            raise ValueError(
                f"Unknown AGENT_MODE '{mode}'. "
                f"Supported: interactive. See status.md for planned additions."
            )
