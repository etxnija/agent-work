from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkspaceHandle:
    path: Path
    branch: str
    _keep: bool = field(default=False, repr=False, init=False)

    def keep(self) -> None:
        """Mark this workspace as successful — the backing branch will not be deleted."""
        self._keep = True


class SandboxRuntime(ABC):
    """
    Provides an isolated workspace for each worker run.
    The loop only talks to this interface — swap backends via AGENT_SANDBOX env var.

    Mirrors the AgentDriver / ApprovalGate pattern so Option 2 (Anthropic sandbox-runtime)
    can be introduced without touching the loop (see ADR-0009).
    """

    @abstractmethod
    def workspace(self, project_root: Path) -> AbstractContextManager[WorkspaceHandle]:
        """
        Return a context manager that sets up and tears down an isolated workspace.

        On enter: yields a WorkspaceHandle(path, branch).
          - path   — directory the worker should use as its cwd
          - branch — backing VCS branch name, or "" if not applicable

        On exit: tears down the workspace.
          - If handle.keep() was called: the backing branch (if any) is preserved
            for a future Phase 3 merge.
          - Otherwise: the branch is discarded along with any uncommitted work.
        """
        ...
