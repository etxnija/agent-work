import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .base import SandboxRuntime, WorkspaceHandle


class GitWorktreeSandbox(SandboxRuntime):
    """
    Git-worktree isolation: each worker run gets its own branch + temporary worktree.

    On success (handle.keep() called): worktree directory is removed, branch is kept
    so Phase 3 can merge it back.
    On failure: both the worktree and branch are discarded — cleanup is a one-liner.
    """

    @contextmanager
    def workspace(self, project_root: Path):
        branch = f"agent/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        worktree_path = Path(tempfile.mkdtemp(prefix="agent-worktree-"))

        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree_path)],
            cwd=project_root,
            check=True,
            capture_output=True,
        )

        handle = WorkspaceHandle(path=worktree_path, branch=branch)
        try:
            yield handle
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=project_root,
                capture_output=True,
            )
            if not handle._keep:
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    cwd=project_root,
                    capture_output=True,
                )
            else:
                print(f"[sandbox] Changes in branch '{branch}' — merge when ready.")
