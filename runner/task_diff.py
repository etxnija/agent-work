"""
Pure, git-free helpers that shape a full `git diff` into a bounded string for
the reviewer: a stat header, a rename block, and a patch section whose line
budget is shared fairly across files (water-filling) instead of being spent
top-down on whichever file git lists first.
"""

MIN_FILE_LINES = 30
DIFF_HEADER = "diff --git "


def split_blocks(patch: str) -> list[list[str]]:
    """Split a full git diff into per-file line lists, in git's order."""
    blocks: list[list[str]] = []
    for line in patch.splitlines():
        if line.startswith(DIFF_HEADER):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return blocks


def rename_pair(block: list[str]) -> tuple[str, str] | None:
    """Return (old, new) when the block is a pure rename (100% similar, no hunks)."""
    if "similarity index 100%" not in block:
        return None
    if any(line.startswith("@@") for line in block):
        return None
    old = next((l[len("rename from "):] for l in block if l.startswith("rename from ")), None)
    new = next((l[len("rename to "):] for l in block if l.startswith("rename to ")), None)
    if old is None or new is None:
        return None
    return old, new


def block_path(block: list[str]) -> str:
    """Path of the file a block describes (new-side path)."""
    for line in block:
        if line.startswith("+++ b/"):
            return line[len("+++ b/"):]
    header = block[0][len(DIFF_HEADER):]
    # `a/X b/X` is 2*len(X)+5 chars; exact when both sides share a path.
    half = (len(header) - 5) // 2
    if half > 0 and header == f"a/{header[2:2 + half]} b/{header[2:2 + half]}":
        return header[2:2 + half]
    return header.rsplit(" b/", 1)[-1]


def allocate(lengths: list[int], budget: int) -> list[int]:
    """
    Water-filling allocation of `budget` lines over blocks of the given lengths.

    Returns one allocation per input, in input order; 0 means "not shown".
    Only the first `budget // MIN_FILE_LINES` blocks are shown. Blocks that
    fit within the current fair share are shown in full and return their
    unused lines to the pool; once none fits, the rest split what remains
    (floored, with the remainder going to the earliest blocks).
    """
    shown = min(len(lengths), max(budget, 0) // MIN_FILE_LINES)
    alloc = [0] * len(lengths)
    pending = list(range(shown))
    remaining = budget
    while pending:
        fits = [i for i in pending if lengths[i] * len(pending) <= remaining]
        if not fits:
            break
        for i in fits:
            alloc[i] = lengths[i]
            remaining -= lengths[i]
        pending = [i for i in pending if i not in fits]
    if pending:
        share, extra = divmod(remaining, len(pending))
        for rank, i in enumerate(pending):
            alloc[i] = share + (1 if rank < extra else 0)
    return alloc


def truncate_block(block: list[str], limit: int) -> list[str]:
    """Keep the header and leading patch lines so block plus marker fits in `limit`."""
    if len(block) <= limit:
        return block
    kept = block[:max(limit - 1, 0)]
    omitted = len(block) - len(kept)
    return kept + [f"... [file truncated: {omitted} additional lines omitted]"]


def build_task_diff(patch: str, stat: str, max_lines: int) -> str:
    """Assemble stat header, NOTE, rename block, budgeted patches and unshown files."""
    renames: list[tuple[str, str]] = []
    files: list[list[str]] = []
    for block in split_blocks(patch):
        pair = rename_pair(block)
        if pair:
            renames.append(pair)
        else:
            files.append(block)

    alloc = allocate([len(b) for b in files], max_lines)
    patch_blocks: list[str] = []
    truncated = 0
    not_shown: list[str] = []
    for block, limit in zip(files, alloc):
        if limit == 0:
            not_shown.append(block_path(block))
            continue
        if len(block) > limit:
            truncated += 1
        patch_blocks.extend(truncate_block(block, limit))

    parts = [stat.rstrip("\n")]
    if truncated or not_shown:
        parts.append(
            f"NOTE: partial diff — {truncated} file(s) truncated, "
            f"{len(not_shown)} file(s) not shown. See the stat above for the full change."
        )
    if renames:
        parts.append("\n".join(["Renamed without changes:"] + [f"{o} -> {n}" for o, n in renames]))
    if patch_blocks:
        parts.append("\n".join(patch_blocks))
    if not_shown:
        parts.append("Files not shown (diff budget exceeded): " + ", ".join(not_shown))
    return "\n".join(_spaced(parts))


def _spaced(parts: list[str]) -> list[str]:
    """Blank line between the sections, but NOTE sits directly beneath the stat."""
    out: list[str] = []
    for i, part in enumerate(parts):
        if i and not part.startswith("NOTE:"):
            out.append("")
        out.append(part)
    return out
