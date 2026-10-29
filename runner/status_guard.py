"""
status_guard.py — deterministic append-only enforcement for memory/status.md.

The Worker is told to only append to status.md, but nothing stopped it from
rewriting or deleting earlier entries. These helpers snapshot the file before
the Worker runs and restore the original content afterwards, keeping any
genuinely new lines. No LLM call, no git call.
"""

import logging
from pathlib import Path

logger = logging.getLogger("agent_loop")


def read_status_text(path: str | Path) -> str | None:
    """Return the file's UTF-8 text, or None if absent or unreadable. Never raises."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        logger.warning(f"[guard] cannot read {path}; skipping append-only check")
        return None


def enforce_append_only(path: str | Path, before_text: str | None) -> int | None:
    """
    Restore `before_text` if the file no longer starts with it.

    Returns None when nothing was done, otherwise the number of new lines kept
    after the restore. Never raises.
    """
    if before_text is None:
        return None
    p = Path(path)

    try:
        after = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0 if _write(p, before_text) else None
    except (OSError, UnicodeDecodeError):
        logger.warning(f"[guard] cannot read {path}; skipping append-only check")
        return None

    if after.startswith(before_text):
        return None

    old_lines = set(before_text.splitlines())
    kept = [line for line in after.splitlines() if line not in old_lines]
    separator = "\n" if before_text and not before_text.endswith("\n") else ""
    restored = before_text + separator
    if kept:
        restored += "\n".join(kept) + "\n"
    return len(kept) if _write(p, restored) else None


def _write(path: Path, text: str) -> bool:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        logger.warning(f"[guard] cannot restore {path}; append-only check not enforced")
        return False
    return True
