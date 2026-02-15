"""Tests for egregore-mcp session-log tools.

Covers spec test scenarios:
1. session_log_append with single entry
2. session_log_append with multiple entries
3. session_log_append with no existing session file (creates new)
4. session_log_append with entry that already has timestamp
5. session_log_read_recent with existing file
6. session_log_read_recent with no file
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

# Must set env BEFORE importing server
TEMP_DIR = tempfile.mkdtemp(prefix="egregore-test-")
os.environ["EGREGORE_ACTIVITY_DIR"] = TEMP_DIR
os.environ["EGREGORE_DEFAULT_PROJECT"] = "test-project"

import server


def run(coro):
    return asyncio.run(coro)


def setup_session_file(project="test-project", content=None):
    """Create a test session file and return its path."""
    from datetime import datetime
    project_dir = Path(TEMP_DIR) / project
    project_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    filename = f"{now.strftime('%Y-%m-%d-%H%M')}-session.md"
    filepath = project_dir / filename
    if content is None:
        content = f"# Session - {now.strftime('%Y-%m-%d %H:%M')} ({project})\n\n[{now.strftime('%Y-%m-%d %H:%M')}] Start\n"
    filepath.write_text(content)
    return filepath


def test_append_single_entry():
    """Scenario 1: single entry appended, timestamp auto-added."""
    fp = setup_session_file()
    result = run(server.session_log_append(
        entries="[TASK] Test single entry",
        session_file=str(fp),
    ))
    assert "1 entry" in result, f"Expected 1 entry confirmation, got: {result}"
    content = fp.read_text()
    assert "[TASK] Test single entry" in content
    # Should have auto-prepended timestamp
    lines = content.strip().split("\n")
    last = lines[-1]
    assert last.startswith("["), f"Expected timestamp prefix, got: {last}"
    print("PASS: test_append_single_entry")


def test_append_multiple_entries():
    """Scenario 2: multiple entries appended, each timestamped."""
    fp = setup_session_file()
    result = run(server.session_log_append(
        entries="[TASK] Entry one\n[DEC] Entry two\n[RESEARCH] Entry three",
        session_file=str(fp),
    ))
    assert "3 entry" in result, f"Expected 3 entries, got: {result}"
    content = fp.read_text()
    assert "[TASK] Entry one" in content
    assert "[DEC] Entry two" in content
    assert "[RESEARCH] Entry three" in content
    print("PASS: test_append_multiple_entries")


def test_append_creates_new_file():
    """Scenario 3: no existing session file → new file created."""
    # Use a fresh project with no files
    new_project = "new-test-project"
    project_dir = Path(TEMP_DIR) / new_project
    if project_dir.exists():
        shutil.rmtree(project_dir)

    result = run(server.session_log_append(
        entries="[TASK] First entry in new project",
        project=new_project,
    ))
    assert "1 entry" in result, f"Expected 1 entry, got: {result}"
    # Verify file was created
    files = list(project_dir.glob("*-session.md"))
    assert len(files) == 1, f"Expected 1 session file, found: {files}"
    content = files[0].read_text()
    assert "# Session -" in content
    assert "[TASK] First entry in new project" in content
    print("PASS: test_append_creates_new_file")


def test_append_preserves_existing_timestamp():
    """Scenario 4: entry with timestamp → timestamp not doubled."""
    fp = setup_session_file()
    entry_with_ts = "[2026-01-01 12:00] [TASK] Already timestamped"
    result = run(server.session_log_append(
        entries=entry_with_ts,
        session_file=str(fp),
    ))
    content = fp.read_text()
    lines = content.strip().split("\n")
    last = lines[-1]
    assert last == entry_with_ts, f"Timestamp was modified: {last}"
    print("PASS: test_append_preserves_existing_timestamp")


def test_read_recent_existing_file():
    """Scenario 5: read recent lines from existing file."""
    fp = setup_session_file()
    # Append some entries
    run(server.session_log_append(
        entries="[TASK] Line A\n[TASK] Line B\n[TASK] Line C",
        session_file=str(fp),
    ))
    result = run(server.session_log_read_recent(
        lines=2,
        session_file=str(fp),
    ))
    lines = result.strip().split("\n")
    assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}: {lines}"
    assert "Line B" in lines[0]
    assert "Line C" in lines[1]
    print("PASS: test_read_recent_existing_file")


def test_read_recent_no_file():
    """Scenario 6: no session file → helpful error message."""
    result = run(server.session_log_read_recent(
        project="nonexistent-project",
    ))
    assert "No session log file found" in result, f"Expected error message, got: {result}"
    print("PASS: test_read_recent_no_file")


def test_append_empty_entries():
    """Edge case: empty entries string."""
    fp = setup_session_file()
    result = run(server.session_log_append(
        entries="",
        session_file=str(fp),
    ))
    assert "No entries" in result, f"Expected no-op, got: {result}"
    print("PASS: test_append_empty_entries")


def test_read_recent_default_lines():
    """Verify default lines=10 works."""
    fp = setup_session_file()
    result = run(server.session_log_read_recent(session_file=str(fp)))
    assert "Start" in result
    print("PASS: test_read_recent_default_lines")


if __name__ == "__main__":
    try:
        test_append_single_entry()
        test_append_multiple_entries()
        test_append_creates_new_file()
        test_append_preserves_existing_timestamp()
        test_read_recent_existing_file()
        test_read_recent_no_file()
        test_append_empty_entries()
        test_read_recent_default_lines()
        print("\n=== ALL TESTS PASSED ===")
    finally:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
