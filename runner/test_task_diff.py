import pytest

from runner.task_diff import (
    allocate,
    block_path,
    build_task_diff,
    rename_pair,
    split_blocks,
    truncate_block,
)


def _file_block(name: str, body_lines: int) -> str:
    head = [f"diff --git a/{name} b/{name}", "index 111..222 100644", f"--- a/{name}", f"+++ b/{name}"]
    hunk = [f"@@ -1,{body_lines} +1,{body_lines} @@"] + [f"+l{i}" for i in range(body_lines - 1)]
    return "\n".join(head + hunk) + "\n"


RENAME = (
    "diff --git a/old.py b/new.py\n"
    "similarity index 100%\n"
    "rename from old.py\n"
    "rename to new.py\n"
)


class TestSplitBlocks:
    @pytest.mark.parametrize("patch,expected_paths", [
        ("", []),
        (_file_block("a.py", 3), ["a.py"]),
        (_file_block("b.py", 3) + _file_block("a.py", 3), ["b.py", "a.py"]),
    ])
    def test_split_preserves_git_order(self, patch, expected_paths):
        assert [block_path(b) for b in split_blocks(patch)] == expected_paths

    def test_lines_stay_with_their_block(self):
        blocks = split_blocks(_file_block("a.py", 3) + _file_block("b.py", 5))
        assert [len(b) for b in blocks] == [7, 9]


class TestRenamePair:
    @pytest.mark.parametrize("patch,expected", [
        (RENAME, ("old.py", "new.py")),
        (RENAME + "@@ -1 +1 @@\n-x\n+y\n", None),
        (
            (
                "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\n"
                "rename to new.py\n--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-x\n+y\n"
            ),
            None,
        ),
        (_file_block("a.py", 3), None),
        ("diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\n", None),
    ])
    def test_classification(self, patch, expected):
        assert rename_pair(split_blocks(patch)[0]) == expected

    def test_block_path_for_pure_rename_uses_new_side(self):
        assert block_path(split_blocks(RENAME)[0]) == "new.py"


class TestBlockPath:
    @pytest.mark.parametrize("header,expected", [
        ("diff --git a/bin.png b/bin.png", "bin.png"),           # no +++ line, same path both sides
        ("diff --git a/old.py b/new.py", "new.py"),              # no +++ line, differing paths
    ])
    def test_without_plus_line(self, header, expected):
        assert block_path([header, "Binary files differ"]) == expected


class TestAllocate:
    @pytest.mark.parametrize("lengths,budget,expected", [
        ([], 100, []),
        ([10, 20, 30], 500, [10, 20, 30]),                 # all small -> all full
        ([400, 10, 10], 100, [80, 10, 10]),                # one huge, small ones full
        ([400, 400], 100, [50, 50]),
        ([400, 400, 400], 100, [34, 33, 33]),              # remainder to earliest
        ([10] * 5, 60, [10, 10, 0, 0, 0]),                 # budget // 30 = 2 shown
        ([100, 100, 100], 59, [59, 0, 0]),                 # only one file fits the minimum
        ([100], 20, [0]),                                  # under the minimum
    ])
    def test_water_filling(self, lengths, budget, expected):
        alloc = allocate(lengths, budget)
        assert alloc == expected
        assert sum(alloc) <= budget

    def test_deterministic(self):
        assert allocate([90, 5, 200, 40], 120) == allocate([90, 5, 200, 40], 120)


class TestTruncateBlock:
    def test_fits_unchanged(self):
        block = ["a", "b", "c"]
        assert truncate_block(block, 3) == block

    def test_truncated_fits_with_marker(self):
        block = [f"l{i}" for i in range(50)]
        out = truncate_block(block, 10)
        assert len(out) == 10
        assert out[:9] == block[:9]
        assert out[-1] == "... [file truncated: 41 additional lines omitted]"


class TestBuildTaskDiff:
    STAT = " a.py | 3 ++-\n 1 file changed\n"

    def test_small_diff_has_stat_and_no_note(self):
        out = build_task_diff(_file_block("a.py", 3), self.STAT, 500)
        assert out.startswith(" a.py | 3 ++-")
        assert "NOTE:" not in out
        assert "+l0" in out

    def test_note_counts_and_position(self):
        patch = _file_block("huge.py", 300) + _file_block("s.py", 5) + _file_block("x.py", 5)
        out = build_task_diff(patch, self.STAT, 60)
        lines = out.splitlines()
        stat_len = len(self.STAT.splitlines())
        assert lines[stat_len] == (
            "NOTE: partial diff — 1 file(s) truncated, 1 file(s) not shown. "
            "See the stat above for the full change."
        )
        assert "Files not shown (diff budget exceeded): x.py" in lines

    def test_too_many_files_lists_the_rest(self):
        patch = "".join(_file_block(f"f{i}.py", 5) for i in range(5))
        out = build_task_diff(patch, self.STAT, 60)
        assert "Files not shown (diff budget exceeded): f2.py, f3.py, f4.py" in out
        assert "3 file(s) not shown" in out

    def test_pure_rename_block(self):
        out = build_task_diff(RENAME + _file_block("a.py", 3), self.STAT, 500)
        assert "Renamed without changes:\nold.py -> new.py" in out
        assert "NOTE:" not in out
        assert "diff --git a/old.py" not in out
