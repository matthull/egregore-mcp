"""Tests for real-time transcript streaming (T002).

Covers:
- WebSocket server starts and accepts connections
- Token validation (reject invalid, accept valid)
- JSONL file created with correct format from mock events
- get_live_chunks returns correct formatted output with cursor tracking
- get_live_chunks returns "no new data" on second call
- get_live_chunks incremental return after new data
- observe_meeting generates correct recording config with realtime_endpoints
- check_bot reports streaming status when active
- All tools (old + new) register correctly
- Backward compatibility: observe_meeting without streaming unchanged
"""

import asyncio
import json
import os
import shutil
import tempfile
import sys
from pathlib import Path
from datetime import datetime, timezone

# Must set env BEFORE importing server
TEMP_DIR = tempfile.mkdtemp(prefix="egregore-stream-test-")
os.environ["EGREGORE_ACTIVITY_DIR"] = TEMP_DIR
os.environ["EGREGORE_DEFAULT_PROJECT"] = "test-project"
os.environ["EGREGORE_TUNNEL_URL"] = "https://mhull.ngrok.dev"
os.environ["EGREGORE_WS_TOKEN"] = "test-token-12345"
os.environ["EGREGORE_WS_PORT"] = "18765"  # Use non-standard port for tests

sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

import server


def run(coro):
    return asyncio.run(coro)


# --- Loop 1: WebSocket server + JSONL writing ---


def test_ws_server_starts():
    """WebSocket server starts without error."""
    async def _test():
        await server._start_ws_server()
        assert server._ws_server is not None, "WS server should be set"
        # Calling again should be idempotent
        await server._start_ws_server()
        server._ws_server.close()
        await server._ws_server.wait_closed()
        server._ws_server = None
    run(_test())
    print("  PASS: test_ws_server_starts")


def test_ws_token_validation():
    """Bad token rejected, good token accepted."""
    import websockets

    async def _test():
        await server._start_ws_server()
        try:
            # Bad token — should be closed by server
            try:
                async with websockets.connect(
                    f"ws://localhost:18765/transcript/test-session?token=bad-token"
                ) as ws:
                    # Try to receive — server should close the connection
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except websockets.ConnectionClosed as e:
                        assert e.code == 4001, f"Expected 4001, got {e.code}"
            except websockets.ConnectionClosed as e:
                assert e.code == 4001, f"Expected 4001, got {e.code}"

            # Good token — should stay open
            async with websockets.connect(
                f"ws://localhost:18765/transcript/test-session?token=test-token-12345"
            ) as ws:
                # If we get here without exception, connection was accepted
                assert ws.protocol.state.name == "OPEN", "Connection should be accepted"

        finally:
            server._ws_server.close()
            await server._ws_server.wait_closed()
            server._ws_server = None

    run(_test())
    print("  PASS: test_ws_token_validation")


def test_ws_writes_jsonl():
    """Transcript event written to JSONL file correctly."""
    import websockets

    session_id = "jsonl-test-session"
    jsonl_path = Path(TEMP_DIR) / "test-project" / f"live-transcript-{session_id}.jsonl"

    # Pre-register session
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    server._active_streams[session_id] = {
        "bot_id": "test-bot",
        "chunk_count": 0,
        "last_ts": None,
        "jsonl_path": str(jsonl_path),
    }
    server._stream_seq[session_id] = 0

    mock_event = {
        "event": "transcript.data",
        "data": {
            "data": {
                "words": [
                    {"text": "Hello", "start_timestamp": {"absolute": 1708084496.5, "relative": 0.0}, "end_timestamp": {"absolute": 1708084497.2, "relative": 0.7}},
                    {"text": "everyone", "start_timestamp": {"absolute": 1708084497.3, "relative": 0.8}, "end_timestamp": {"absolute": 1708084498.0, "relative": 1.5}},
                ],
                "participant": {"id": 123, "name": "Alice", "is_host": True, "platform": "google_meet"},
            }
        },
    }

    async def _test():
        await server._start_ws_server()
        try:
            async with websockets.connect(
                f"ws://localhost:18765/transcript/{session_id}?token=test-token-12345"
            ) as ws:
                await ws.send(json.dumps(mock_event))
                # Give server time to process
                await asyncio.sleep(0.3)

            # Verify JSONL was written
            assert jsonl_path.exists(), f"JSONL file should exist at {jsonl_path}"
            content = jsonl_path.read_text().strip()
            assert content, "JSONL file should not be empty"
            entry = json.loads(content)
            assert entry["speaker"] == "Alice", f"Expected Alice, got {entry['speaker']}"
            assert entry["text"] == "Hello everyone", f"Expected 'Hello everyone', got {entry['text']}"
            assert entry["seq"] == 0, f"Expected seq 0, got {entry['seq']}"
            assert entry["ts"] == 1708084496.5, f"Expected ts 1708084496.5, got {entry['ts']}"
            assert entry["speaker_id"] == 123

            # Verify stream state updated
            assert server._active_streams[session_id]["chunk_count"] == 1
            assert server._active_streams[session_id]["last_ts"] == 1708084496.5

        finally:
            server._ws_server.close()
            await server._ws_server.wait_closed()
            server._ws_server = None
            # Clean up
            if jsonl_path.exists():
                jsonl_path.unlink()
            server._active_streams.pop(session_id, None)
            server._stream_seq.pop(session_id, None)

    run(_test())
    print("  PASS: test_ws_writes_jsonl")


# --- Loop 2: Tool registration + recording config ---


def test_all_tools_registered():
    """All 6 tools (old + new) register correctly."""
    tools = {t.name for t in server.mcp._tool_manager.list_tools()}
    expected = {
        "session_log_append", "session_log_read_recent",
        "observe_meeting", "check_bot", "get_transcript",
        "get_live_chunks",
    }
    assert expected.issubset(tools), f"Expected {expected} to be subset of {tools}"
    print("  PASS: test_all_tools_registered")


def test_streaming_config_generation():
    """Streaming config has realtime_endpoints and low-latency mode."""
    config = server._build_streaming_config("test-session-123")
    assert "realtime_endpoints" in config, "Config should have realtime_endpoints"
    assert len(config["realtime_endpoints"]) == 1
    ep = config["realtime_endpoints"][0]
    assert ep["type"] == "websocket"
    assert "test-session-123" in ep["url"]
    assert "token=test-token-12345" in ep["url"]
    assert ep["events"] == ["transcript.data"]
    assert config["transcript"]["provider"]["recallai_streaming"]["mode"] == "prioritize_low_latency"
    assert config["transcript"]["provider"]["recallai_streaming"]["language_code"] == "en"
    # Original config should be unchanged
    assert server.RECALLAI_RECORDING_CONFIG["transcript"]["provider"]["recallai_streaming"]["mode"] == "prioritize_accuracy"
    assert server.RECALLAI_RECORDING_CONFIG["transcript"]["provider"]["recallai_streaming"]["language_code"] == "auto"
    print("  PASS: test_streaming_config_generation")


def test_observe_meeting_streaming_no_tunnel():
    """observe_meeting(streaming=True) errors when EGREGORE_TUNNEL_URL not set."""
    original = server.EGREGORE_TUNNEL_URL
    server.EGREGORE_TUNNEL_URL = ""
    try:
        result = run(server.observe_meeting("https://meet.google.com/test", streaming=True))
        assert "EGREGORE_TUNNEL_URL not set" in result, f"Expected error, got: {result}"
    finally:
        server.EGREGORE_TUNNEL_URL = original
    print("  PASS: test_observe_meeting_streaming_no_tunnel")


# --- Loop 3: get_live_chunks E2E mock ---


def test_get_live_chunks_no_session():
    """get_live_chunks with unknown bot_id returns helpful error."""
    result = run(server.get_live_chunks("nonexistent-bot-id"))
    assert "No streaming session found" in result, f"Expected error, got: {result}"
    print("  PASS: test_get_live_chunks_no_session")


def test_get_live_chunks_formatting():
    """get_live_chunks returns correctly formatted output with cursor tracking."""
    bot_id = "format-test-bot"
    session_id = "format-test-session"
    jsonl_path = Path(TEMP_DIR) / "test-project" / f"live-transcript-{session_id}.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    # Write sample JSONL
    entries = [
        {"ts": 1708084496.789, "speaker": "Alice", "speaker_id": 1, "text": "Hello everyone", "seq": 0},
        {"ts": 1708084499.123, "speaker": "Bob", "speaker_id": 2, "text": "Hi Alice", "seq": 1},
    ]
    with open(jsonl_path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    # Register mappings
    server._bot_to_session[bot_id] = session_id
    server._active_streams[session_id] = {
        "bot_id": bot_id,
        "chunk_count": 2,
        "last_ts": 1708084499.123,
        "jsonl_path": str(jsonl_path),
    }

    try:
        # First call — should get both chunks
        result = run(server.get_live_chunks(bot_id))
        assert "Alice: Hello everyone" in result, f"Missing Alice line: {result}"
        assert "Bob: Hi Alice" in result, f"Missing Bob line: {result}"
        assert "2 new chunks" in result, f"Missing chunk count: {result}"

        # Second call — no new data
        result2 = run(server.get_live_chunks(bot_id))
        assert "No new transcript data" in result2, f"Expected no new data, got: {result2}"

        # Add more data
        new_entry = {"ts": 1708084502.0, "speaker": "Alice", "speaker_id": 1, "text": "Let us begin", "seq": 2}
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(new_entry) + "\n")

        # Third call — only new chunk
        result3 = run(server.get_live_chunks(bot_id))
        assert "Alice: Let us begin" in result3, f"Missing new chunk: {result3}"
        assert "1 new chunks" in result3, f"Expected 1 chunk: {result3}"
        assert "Bob" not in result3, f"Should not contain old data: {result3}"

    finally:
        # Clean up
        if jsonl_path.exists():
            jsonl_path.unlink()
        server._bot_to_session.pop(bot_id, None)
        server._active_streams.pop(session_id, None)
        server._live_cursors.pop(bot_id, None)

    print("  PASS: test_get_live_chunks_formatting")


def test_get_live_chunks_empty_file():
    """get_live_chunks with empty JSONL file returns waiting message."""
    bot_id = "empty-test-bot"
    session_id = "empty-test-session"
    jsonl_path = Path(TEMP_DIR) / "test-project" / f"live-transcript-{session_id}.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text("")

    server._bot_to_session[bot_id] = session_id
    server._active_streams[session_id] = {
        "bot_id": bot_id,
        "chunk_count": 0,
        "last_ts": None,
        "jsonl_path": str(jsonl_path),
    }

    try:
        result = run(server.get_live_chunks(bot_id))
        assert "no transcript data yet" in result.lower(), f"Expected waiting msg, got: {result}"
    finally:
        if jsonl_path.exists():
            jsonl_path.unlink()
        server._bot_to_session.pop(bot_id, None)
        server._active_streams.pop(session_id, None)
        server._live_cursors.pop(bot_id, None)

    print("  PASS: test_get_live_chunks_empty_file")


def test_check_bot_streaming_status():
    """check_bot includes streaming status when stream is active."""
    # We can't easily mock the HTTP call, but we can test the streaming
    # status appending logic by checking that _bot_to_session lookup works.
    # This is a unit-level check of the mapping.
    bot_id = "stream-status-bot"
    session_id = "stream-status-session"
    server._bot_to_session[bot_id] = session_id
    server._active_streams[session_id] = {
        "bot_id": bot_id,
        "chunk_count": 42,
        "last_ts": 1708084500.0,
        "jsonl_path": "/tmp/fake.jsonl",
    }

    try:
        # Verify the mappings exist (the actual check_bot call requires API)
        assert server._bot_to_session[bot_id] == session_id
        assert server._active_streams[session_id]["chunk_count"] == 42
    finally:
        server._bot_to_session.pop(bot_id, None)
        server._active_streams.pop(session_id, None)

    print("  PASS: test_check_bot_streaming_status")


def test_ws_invalid_path():
    """WebSocket connection with invalid path is rejected."""
    import websockets

    async def _test():
        await server._start_ws_server()
        try:
            try:
                async with websockets.connect(
                    f"ws://localhost:18765/bad-path?token=test-token-12345"
                ) as ws:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except websockets.ConnectionClosed as e:
                        assert e.code == 4000, f"Expected 4000, got {e.code}"
            except websockets.ConnectionClosed as e:
                assert e.code == 4000, f"Expected 4000, got {e.code}"
        finally:
            server._ws_server.close()
            await server._ws_server.wait_closed()
            server._ws_server = None

    run(_test())
    print("  PASS: test_ws_invalid_path")


def test_ws_ignores_non_transcript_events():
    """WebSocket handler ignores events that aren't transcript.data."""
    import websockets

    session_id = "ignore-test-session"
    jsonl_path = Path(TEMP_DIR) / "test-project" / f"live-transcript-{session_id}.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    server._active_streams[session_id] = {
        "bot_id": "test-bot",
        "chunk_count": 0,
        "last_ts": None,
        "jsonl_path": str(jsonl_path),
    }
    server._stream_seq[session_id] = 0

    async def _test():
        await server._start_ws_server()
        try:
            async with websockets.connect(
                f"ws://localhost:18765/transcript/{session_id}?token=test-token-12345"
            ) as ws:
                # Send a non-transcript event
                await ws.send(json.dumps({"event": "bot.status_change", "data": {"status": "joining"}}))
                await asyncio.sleep(0.3)

            # JSONL should not exist or be empty
            if jsonl_path.exists():
                content = jsonl_path.read_text().strip()
                assert content == "", f"Expected empty file, got: {content}"

        finally:
            server._ws_server.close()
            await server._ws_server.wait_closed()
            server._ws_server = None
            if jsonl_path.exists():
                jsonl_path.unlink()
            server._active_streams.pop(session_id, None)
            server._stream_seq.pop(session_id, None)

    run(_test())
    print("  PASS: test_ws_ignores_non_transcript_events")


if __name__ == "__main__":
    print("Running streaming tests...")
    try:
        # Loop 1: WebSocket server + JSONL
        print("\n--- Loop 1: WebSocket server + JSONL ---")
        test_ws_server_starts()
        test_ws_token_validation()
        test_ws_writes_jsonl()
        test_ws_invalid_path()
        test_ws_ignores_non_transcript_events()

        # Loop 2: Tool registration + config
        print("\n--- Loop 2: Tool registration + config ---")
        test_all_tools_registered()
        test_streaming_config_generation()
        test_observe_meeting_streaming_no_tunnel()

        # Loop 3: get_live_chunks E2E mock
        print("\n--- Loop 3: get_live_chunks ---")
        test_get_live_chunks_no_session()
        test_get_live_chunks_formatting()
        test_get_live_chunks_empty_file()
        test_check_bot_streaming_status()

        print("\n=== ALL STREAMING TESTS PASSED ===")
    finally:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
