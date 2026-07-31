from contextlib import contextmanager
from pathlib import Path

from .base import SandboxRuntime, WorkspaceHandle


class NoopSandbox(SandboxRuntime):
    """
    Pass-through sandbox — the worker runs directly in project_root with no isolation.
    Used in tests and for projects not under git version control.
    """

    @contextmanager
    def workspace(self, project_root: Path):
        yield WorkspaceHandle(path=project_root, branch="")
