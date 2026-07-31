from abc import ABC, abstractmethod


class ApprovalGate(ABC):
    """
    Pauses the loop and asks a human to approve a plan before work starts.

    Two implementations are planned:
    - InteractiveGate: prints plan, waits for y/N at the terminal (Phase 1)
    - FileGate: polls for a plan.approved sentinel file (Phase 4, unattended loops)
      See status.md backlog.

    Select via AGENT_MODE env var or pass explicitly to get_gate().
    """

    @abstractmethod
    def request(self, plan_path: str) -> bool:
        """
        Show the plan and wait for human approval.
        Returns True if approved, False if rejected.
        """
        ...
