import os

from .base import SandboxRuntime, WorkspaceHandle
from .noop import NoopSandbox
from .worktree import GitWorktreeSandbox

__all__ = [
    "GitWorktreeSandbox",
    "NoopSandbox",
    "SandboxRuntime",
    "WorkspaceHandle",
    "get_sandbox",
]


def get_sandbox() -> SandboxRuntime:
    """
    Factory: returns the configured SandboxRuntime.
    Controlled by AGENT_SANDBOX env var (default: worktree).

    Supported values:
      worktree — git worktree per worker run (default, Phase 1.1)
      noop     — no isolation; worker runs in project_root (testing / no-git projects)
    """
    mode = os.environ.get("AGENT_SANDBOX", "worktree")
    match mode:
        case "worktree":
            return GitWorktreeSandbox()
        case "noop":
            return NoopSandbox()
        case _:
            raise ValueError(
                f"Unknown AGENT_SANDBOX '{mode}'. Supported: worktree, noop."
            )
