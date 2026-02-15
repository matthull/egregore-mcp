"""Tests for Recall.ai meeting bot tools. Run with: python test_recall_tools.py"""

import asyncio
import sys
import os

# Ensure we can import server
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

import server


def test_format_transcript_basic():
    sample = [
        {"participant": {"name": "Alice"}, "words": [{"text": "Hello"}, {"text": "everyone"}]},
        {"participant": {"name": "Bob"}, "words": [{"text": "Hi"}, {"text": "Alice"}]},
    ]
    result = server._format_transcript(sample)
    assert "Alice: Hello everyone" in result, f"Expected 'Alice: Hello everyone' in: {result}"
    assert "Bob: Hi Alice" in result, f"Expected 'Bob: Hi Alice' in: {result}"
    print("  PASS: basic formatting")


def test_format_transcript_unknown_speaker():
    sample = [
        {"participant": {}, "words": [{"text": "Testing"}, {"text": "no name"}]},
    ]
    result = server._format_transcript(sample)
    assert "Unknown Speaker: Testing no name" in result, f"Got: {result}"
    print("  PASS: unknown speaker")


def test_format_transcript_empty_words():
    sample = [
        {"participant": {"name": "Carol"}, "words": []},
    ]
    result = server._format_transcript(sample)
    assert result == "", f"Expected empty string, got: {result!r}"
    print("  PASS: empty words excluded")


def test_format_transcript_blank_separator():
    sample = [
        {"participant": {"name": "Alice"}, "words": [{"text": "Hello"}]},
        {"participant": {"name": "Bob"}, "words": [{"text": "Hi"}]},
    ]
    result = server._format_transcript(sample)
    assert result == "Alice: Hello\n\nBob: Hi", f"Got: {result!r}"
    print("  PASS: blank line separator")


def test_tools_registered():
    tools = {t.name for t in server.mcp._tool_manager.list_tools()}
    expected = {"session_log_append", "session_log_read_recent", "observe_meeting", "check_bot", "get_transcript"}
    assert expected == tools, f"Expected {expected}, got {tools}"
    print("  PASS: all 5 tools registered")


def test_bot_status_messages_cover_lifecycle():
    expected_states = [
        "bot.joining_call", "bot.in_waiting_room", "bot.in_call_not_recording",
        "bot.recording_permission_allowed", "bot.in_call_recording",
        "bot.call_ended", "bot.done", "bot.fatal",
    ]
    for state in expected_states:
        assert state in server.BOT_STATUS_MESSAGES, f"Missing state: {state}"
    print("  PASS: all lifecycle states mapped")


async def test_missing_api_key():
    original = server.RECALLAI_API_KEY
    server.RECALLAI_API_KEY = ""
    try:
        r1 = await server.observe_meeting("https://meet.google.com/abc-defg-hij")
        assert "RECALLAI_API_KEY not set" in r1, f"observe_meeting: {r1}"

        r2 = await server.check_bot("some-bot-id")
        assert "RECALLAI_API_KEY not set" in r2, f"check_bot: {r2}"

        r3 = await server.get_transcript("some-bot-id")
        assert "RECALLAI_API_KEY not set" in r3, f"get_transcript: {r3}"
        print("  PASS: missing API key returns clear error")
    finally:
        server.RECALLAI_API_KEY = original


if __name__ == "__main__":
    print("Running Recall.ai tool tests...")
    test_format_transcript_basic()
    test_format_transcript_unknown_speaker()
    test_format_transcript_empty_words()
    test_format_transcript_blank_separator()
    test_tools_registered()
    test_bot_status_messages_cover_lifecycle()
    asyncio.run(test_missing_api_key())
    print("\nAll tests passed!")
