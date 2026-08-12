import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .base import SandboxRuntime, WorkspaceHandle


class GitWorktreeSandbox(SandboxRuntime):
    """
    Git-worktree isolation: each worker run gets its own branch + temporary worktree.

    The worktree directory is always removed on exit. The branch is kept only if
    handle.keep() was called — on success, or on a worker failure or a
    sensor-retry-exhausted failure that still wants completed prior-task commits
    preserved — otherwise it's discarded via `git branch -D`.
    """

    @contextmanager
    def workspace(self, project_root: Path):
        branch = f"agent/{datetime.now(tz=UTC).strftime('%Y%m%d-%H%M%S')}"
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
                check=False,
            )
            if not handle._keep:
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    cwd=project_root,
                    capture_output=True,
                    check=False,
                )
            else:
                print(f"[sandbox] Changes in branch '{branch}' — merge when ready.")
