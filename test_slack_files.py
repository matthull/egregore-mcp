"""Tests for file/image attachment extraction in Slack tools.

Covers:
1. _format_files with no files
2. _format_files with image files
3. _format_files with mixed file types
4. _format_files with missing fields (graceful fallbacks)
5. read_slack_thread includes file attachments
6. scan_channel includes file attachments
"""

import asyncio
import os
from unittest.mock import AsyncMock, patch, MagicMock

# Must set env BEFORE importing server
os.environ.setdefault("EGREGORE_ACTIVITY_DIR", "/tmp/egregore-test")
os.environ.setdefault("EGREGORE_DEFAULT_PROJECT", "test-project")

import server


def run(coro):
    return asyncio.run(coro)


# --- _format_files unit tests ---


def test_format_files_no_files():
    """No files field → empty string."""
    assert server._format_files({}) == ""
    assert server._format_files({"files": []}) == ""
    print("PASS: test_format_files_no_files")


def test_format_files_single_image():
    """Single image file with permalink."""
    msg = {
        "files": [
            {
                "name": "screenshot.png",
                "mimetype": "image/png",
                "filetype": "png",
                "permalink": "https://slack.com/files/U123/F456/screenshot.png",
            }
        ]
    }
    result = server._format_files(msg)
    assert "[image]" in result
    assert "screenshot.png" in result
    assert "https://slack.com/files/U123/F456/screenshot.png" in result
    print("PASS: test_format_files_single_image")


def test_format_files_multiple_images():
    """Multiple image files produce multiple lines."""
    msg = {
        "files": [
            {
                "name": "screen1.png",
                "mimetype": "image/png",
                "permalink": "https://slack.com/files/1",
            },
            {
                "name": "screen2.jpg",
                "mimetype": "image/jpeg",
                "permalink": "https://slack.com/files/2",
            },
        ]
    }
    result = server._format_files(msg)
    lines = result.strip().split("\n")
    assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}: {lines}"
    assert "screen1.png" in lines[0]
    assert "screen2.jpg" in lines[1]
    print("PASS: test_format_files_multiple_images")


def test_format_files_mixed_types():
    """Different file types get correct labels."""
    msg = {
        "files": [
            {"name": "photo.png", "mimetype": "image/png", "permalink": ""},
            {"name": "recording.mp4", "mimetype": "video/mp4", "permalink": ""},
            {"name": "voice.ogg", "mimetype": "audio/ogg", "permalink": ""},
            {"name": "doc.pdf", "mimetype": "application/pdf", "permalink": ""},
            {"name": "data.csv", "mimetype": "text/csv", "filetype": "csv", "permalink": ""},
        ]
    }
    result = server._format_files(msg)
    assert "[image]" in result
    assert "[video]" in result
    assert "[audio]" in result
    assert "[pdf]" in result
    assert "[csv]" in result
    print("PASS: test_format_files_mixed_types")


def test_format_files_missing_fields():
    """Graceful fallback when file fields are missing."""
    msg = {
        "files": [
            {"mimetype": "image/png"},  # no name, no permalink
            {},  # completely empty
        ]
    }
    result = server._format_files(msg)
    lines = result.strip().split("\n")
    assert len(lines) == 2
    assert "unnamed" in lines[0] or "unnamed" in lines[1]
    # No permalink → no " — " separator for that file
    print("PASS: test_format_files_missing_fields")


def test_format_files_title_fallback():
    """Uses title when name is missing."""
    msg = {
        "files": [
            {"title": "My Screenshot", "mimetype": "image/png", "permalink": "https://example.com"},
        ]
    }
    result = server._format_files(msg)
    assert "My Screenshot" in result
    print("PASS: test_format_files_title_fallback")


def test_format_files_no_permalink():
    """File without permalink doesn't include dash separator."""
    msg = {
        "files": [
            {"name": "local.png", "mimetype": "image/png"},
        ]
    }
    result = server._format_files(msg)
    assert "[image] local.png" == result.strip()
    assert "—" not in result
    print("PASS: test_format_files_no_permalink")


# --- Integration tests with mocked Slack API ---


def _mock_slack_client():
    """Create a mock Slack client with conversations_replies and conversations_history."""
    client = AsyncMock()
    client.users_info = AsyncMock(return_value={
        "user": {"real_name": "Test User", "name": "testuser"}
    })
    return client


def test_read_slack_thread_includes_files():
    """read_slack_thread output includes file attachment info."""
    mock_client = _mock_slack_client()
    mock_client.conversations_replies = AsyncMock(return_value={
        "messages": [
            {
                "user": "U123",
                "ts": "1700000000.000000",
                "text": "Here's a screenshot",
                "files": [
                    {
                        "name": "error.png",
                        "mimetype": "image/png",
                        "filetype": "png",
                        "permalink": "https://slack.com/files/U123/F789/error.png",
                    }
                ],
            },
            {
                "user": "U456",
                "ts": "1700000060.000000",
                "text": "No files here",
            },
        ]
    })

    with patch.object(server, "_get_slack_client", return_value=mock_client), \
         patch.object(server, "_resolve_slack_channel", new=AsyncMock(return_value=("C123", "general"))):
        result = run(server.read_slack_thread(channel="general", thread_ts="1700000000.000000"))

    assert "Here's a screenshot" in result
    assert "[image] error.png" in result
    assert "https://slack.com/files/U123/F789/error.png" in result
    # Second message has no files
    assert "No files here" in result
    print("PASS: test_read_slack_thread_includes_files")


def test_read_slack_thread_no_files():
    """read_slack_thread without files works as before."""
    mock_client = _mock_slack_client()
    mock_client.conversations_replies = AsyncMock(return_value={
        "messages": [
            {
                "user": "U123",
                "ts": "1700000000.000000",
                "text": "Plain message",
            },
        ]
    })

    with patch.object(server, "_get_slack_client", return_value=mock_client), \
         patch.object(server, "_resolve_slack_channel", new=AsyncMock(return_value=("C123", "general"))):
        result = run(server.read_slack_thread(channel="general", thread_ts="1700000000.000000"))

    assert "Plain message" in result
    assert "[image]" not in result
    assert "[file]" not in result
    print("PASS: test_read_slack_thread_no_files")


def test_scan_channel_includes_files():
    """scan_channel output includes file attachment info."""
    import time as _time
    recent_ts = str(_time.time())

    mock_client = _mock_slack_client()
    mock_client.conversations_history = AsyncMock(return_value={
        "messages": [
            {
                "user": "U123",
                "ts": recent_ts,
                "text": "Check this out",
                "files": [
                    {
                        "name": "dashboard.png",
                        "mimetype": "image/png",
                        "permalink": "https://slack.com/files/dash.png",
                    }
                ],
            },
        ],
        "response_metadata": {"next_cursor": ""},
    })

    with patch.object(server, "_get_slack_client", return_value=mock_client), \
         patch.object(server, "_resolve_slack_channel", new=AsyncMock(return_value=("C123", "general"))):
        result = run(server.scan_channel(channel="general", hours_back=24))

    assert "Check this out" in result
    assert "[image] dashboard.png" in result
    assert "https://slack.com/files/dash.png" in result
    print("PASS: test_scan_channel_includes_files")


if __name__ == "__main__":
    test_format_files_no_files()
    test_format_files_single_image()
    test_format_files_multiple_images()
    test_format_files_mixed_types()
    test_format_files_missing_fields()
    test_format_files_title_fallback()
    test_format_files_no_permalink()
    test_read_slack_thread_includes_files()
    test_read_slack_thread_no_files()
    test_scan_channel_includes_files()
    print("\n=== ALL SLACK FILE TESTS PASSED ===")
