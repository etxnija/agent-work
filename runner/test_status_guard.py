"""Tests for runner/status_guard.py."""

import os

import pytest

from runner.status_guard import appended_text, enforce_append_only, read_status_text

BEFORE = "- one\n- two\n- three\n"

# (case id, content after the Worker ran, expected return, expected file text)
CHANGE_CASES = [
    ("append-only", BEFORE + "- four\n", None, BEFORE + "- four\n"),
    ("unchanged", BEFORE, None, BEFORE),
    ("edited-middle", "- one\n- TWO\n- three\n- four\n", 2, BEFORE + "- TWO\n- four\n"),
    ("truncated", "- one\n", 0, BEFORE),
    ("emptied", "", 0, BEFORE),
]


@pytest.mark.parametrize(
    ("after", "expected", "final"),
    [c[1:] for c in CHANGE_CASES],
    ids=[c[0] for c in CHANGE_CASES],
)
def test_enforce_append_only(tmp_path, after, expected, final):
    path = tmp_path / "status.md"
    path.write_text(after)
    assert enforce_append_only(path, BEFORE) == expected
    assert path.read_text() == final


def test_before_without_trailing_newline_gets_separator(tmp_path):
    path = tmp_path / "status.md"
    path.write_text("- X\n- new\n")
    assert enforce_append_only(path, "- one") == 2
    assert path.read_text() == "- one\n- X\n- new\n"


def test_deleted_file_is_restored(tmp_path):
    path = tmp_path / "status.md"
    assert enforce_append_only(path, BEFORE) == 0
    assert path.read_text() == BEFORE


def test_absent_before_and_after(tmp_path):
    path = tmp_path / "status.md"
    assert enforce_append_only(path, None) is None
    assert not path.exists()


def test_absent_before_present_after(tmp_path):
    path = tmp_path / "status.md"
    path.write_text("- new\n")
    assert enforce_append_only(path, None) is None
    assert path.read_text() == "- new\n"


def test_non_utf8_after_is_left_alone(tmp_path):
    path = tmp_path / "status.md"
    path.write_bytes(b"\xff\xfe\x00bad")
    assert enforce_append_only(path, BEFORE) is None
    assert path.read_bytes() == b"\xff\xfe\x00bad"


def test_directory_at_path_does_not_raise(tmp_path):
    path = tmp_path / "status.md"
    path.mkdir()
    assert enforce_append_only(path, BEFORE) is None
    assert path.is_dir()
    assert read_status_text(path) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_file_does_not_raise(tmp_path):
    path = tmp_path / "status.md"
    path.write_text("- changed\n")
    path.chmod(0)
    try:
        assert enforce_append_only(path, BEFORE) is None
        assert read_status_text(path) is None
    finally:
        path.chmod(0o644)
    assert path.read_text() == "- changed\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unsearchable_parent_does_not_raise(tmp_path):
    parent = tmp_path / "locked"
    parent.mkdir()
    path = parent / "status.md"
    path.write_text("- changed\n")
    parent.chmod(0)
    try:
        assert enforce_append_only(path, BEFORE) is None
    finally:
        parent.chmod(0o755)
    assert path.read_text() == "- changed\n"


def test_restore_write_failure_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / "status.md"
    path.write_text("- changed\n")

    def fail(self, *args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(type(path), "write_text", fail)
    assert enforce_append_only(path, BEFORE) is None
    assert path.read_text() == "- changed\n"


def test_appended_line_duplicating_existing_line_is_dropped(tmp_path):
    """Known limitation: 'new' means text not already in the file, so repeats are lost."""
    path = tmp_path / "status.md"
    path.write_text("- two\n- fresh\n- two\n")
    assert enforce_append_only(path, BEFORE) == 1
    assert path.read_text() == BEFORE + "- fresh\n"


def test_read_status_text(tmp_path):
    path = tmp_path / "status.md"
    assert read_status_text(path) is None
    path.write_text(BEFORE)
    assert read_status_text(path) == BEFORE


# (case id, before, content now, expected appended text)
APPENDED_CASES = [
    ("pure-append", BEFORE, BEFORE + "- four\n", "- four\n"),
    ("unchanged", BEFORE, BEFORE, ""),
    ("before-without-trailing-newline", "- one", "- one\n- new\n", "\n- new\n"),
    ("rewrite", BEFORE, "- one\n- TWO\n- three\n- four\n", "- TWO\n- four"),
    ("emptied", BEFORE, "", ""),
    ("rewrite-inserting-blank-line", BEFORE, "- one\n\n- TWO\n  \n- three\n- four\n", "- TWO\n- four"),
]


@pytest.mark.parametrize(
    ("before", "now", "expected"),
    [c[1:] for c in APPENDED_CASES],
    ids=[c[0] for c in APPENDED_CASES],
)
def test_appended_text(tmp_path, before, now, expected):
    path = tmp_path / "status.md"
    path.write_text(now)
    assert appended_text(path, before) == expected


def test_appended_text_missing_file(tmp_path):
    assert appended_text(tmp_path / "nope.md", BEFORE) == ""


def test_appended_text_none_before(tmp_path):
    path = tmp_path / "status.md"
    path.write_text("- x\n")
    assert appended_text(path, None) == ""
