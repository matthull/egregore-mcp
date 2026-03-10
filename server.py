"""egregore-mcp: Unified MCP server for egregore tools.

Features:
- Session-log tools for zero-friction session logging
- Recall.ai meeting bot tools for dispatching bots and retrieving transcripts
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uuid

import httpx
import websockets
from dotenv import load_dotenv
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

load_dotenv()

logger = logging.getLogger("egregore-mcp")
logger.setLevel(logging.DEBUG)
# Log to stderr, NOT stdout (stdout is MCP protocol)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

ACTIVITY_DIR = Path(os.getenv("EGREGORE_ACTIVITY_DIR", str(Path.home() / "egregore" / "activity")))
DEFAULT_PROJECT = os.getenv("EGREGORE_DEFAULT_PROJECT", "village")

TIMESTAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]")

# Recall.ai configuration
RECALLAI_API_KEY = os.getenv("RECALLAI_API_KEY", "")
RECALLAI_REGION = os.getenv("RECALLAI_REGION", "us-west-2")
RECALLAI_BASE_URL = f"https://{RECALLAI_REGION}.recall.ai/api/v1"

RECALLAI_RECORDING_CONFIG = {
    "transcript": {
        "provider": {
            "recallai_streaming": {
                "language_code": "auto",
                "mode": "prioritize_accuracy",
            }
        },
        "diarization": {
            "use_separate_streams_when_available": True,
        },
    },
    "automatic_leave": {
        "everyone_left_timeout": 30,
        "waiting_room_timeout": 600,
    },
}

# Slack configuration
SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_WRITE_CHANNELS = [c.strip() for c in os.getenv("SLACK_WRITE_CHANNELS", "").split(",") if c.strip()]
SLACK_TAG_EMOJI = os.getenv("SLACK_TAG_EMOJI", "robot_face")
SLACK_TAGGED_THREADS_FILE = Path(os.getenv("SLACK_TAGGED_THREADS_FILE", str(Path.home() / ".slack-tagged-threads.jsonl")))

_slack_user_client: AsyncWebClient | None = None
_slack_bot_client: AsyncWebClient | None = None
_slack_channel_cache: dict[str, str] = {}  # name -> ID and ID -> name


def _get_slack_client() -> AsyncWebClient:
    """Get or create the Slack user client (xoxp — for read/search operations)."""
    global _slack_user_client
    if _slack_user_client is None:
        if not SLACK_USER_TOKEN:
            raise ValueError("SLACK_USER_TOKEN not set. Add it to your .env file.")
        _slack_user_client = AsyncWebClient(token=SLACK_USER_TOKEN)
    return _slack_user_client


def _get_slack_bot_client() -> AsyncWebClient:
    """Get or create the Slack bot client (xoxb — for posting messages as the bot)."""
    global _slack_bot_client
    if _slack_bot_client is None:
        if not SLACK_BOT_TOKEN:
            raise ValueError("SLACK_BOT_TOKEN not set. Add it to your .env file.")
        _slack_bot_client = AsyncWebClient(token=SLACK_BOT_TOKEN)
    return _slack_bot_client


async def _resolve_slack_channel(channel: str) -> tuple[str, str | None]:
    """Resolve a channel name or ID to (channel_id, channel_name).

    Accepts '#channel-name', 'channel-name', or 'C...' IDs.
    Returns (channel_id, channel_name) or (original_input, None) if unresolved.
    """
    channel = channel.strip().lstrip("#")

    # Already an ID (starts with C or G and is alphanumeric)
    if len(channel) > 5 and channel[0] in ("C", "G") and channel.isalnum():
        if channel in _slack_channel_cache:
            return channel, _slack_channel_cache[channel]
        # Look up name from ID
        try:
            client = _get_slack_client()
            resp = await client.conversations_info(channel=channel)
            name = resp["channel"]["name"]
            _slack_channel_cache[channel] = name
            _slack_channel_cache[name] = channel
            return channel, name
        except SlackApiError:
            return channel, None

    # It's a name — check cache first
    if channel in _slack_channel_cache:
        cid = _slack_channel_cache[channel]
        return cid, channel

    # Search for channel by name via conversations_list
    try:
        client = _get_slack_client()
        cursor = None
        for _ in range(5):  # max 5 pages
            kwargs: dict = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await client.conversations_list(**kwargs)
            for ch in resp.get("channels", []):
                _slack_channel_cache[ch["name"]] = ch["id"]
                _slack_channel_cache[ch["id"]] = ch["name"]
                if ch["name"] == channel:
                    return ch["id"], ch["name"]
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except SlackApiError:
        pass

    # Fallback: search for a message in the channel to extract channel ID
    # conversations_list doesn't always return all channels (Connect channels, etc.)
    try:
        client = _get_slack_client()
        resp = await client.search_messages(query=f"in:#{channel}", count=1)
        matches = resp.get("messages", {}).get("matches", [])
        if matches:
            ch_info = matches[0].get("channel", {})
            cid = ch_info.get("id")
            cname = ch_info.get("name")
            if cid and cname:
                _slack_channel_cache[cname] = cid
                _slack_channel_cache[cid] = cname
                return cid, cname
    except SlackApiError:
        pass

    return channel, None


async def _resolve_whitelist_names() -> list[str]:
    """Return writable channel names (resolved from SLACK_WRITE_CHANNELS IDs)."""
    names = []
    for cid in SLACK_WRITE_CHANNELS:
        _, name = await _resolve_slack_channel(cid)
        names.append(f"#{name}" if name else cid)
    return names


# Streaming configuration
EGREGORE_WS_PORT = int(os.getenv("EGREGORE_WS_PORT", "8765"))
EGREGORE_WS_TOKEN = os.getenv("EGREGORE_WS_TOKEN", "") or secrets.token_urlsafe(32)
EGREGORE_TUNNEL_URL = os.getenv("EGREGORE_TUNNEL_URL", "")

# Module-level streaming state
_ws_server: asyncio.Server | None = None
_live_cursors: dict[str, int] = {}  # bot_id → last seq seen
_stream_seq: dict[str, int] = {}  # session_id → next seq to assign
_active_streams: dict[str, dict] = {}  # session_id → {bot_id, chunk_count, last_ts, jsonl_path}
_session_to_bot: dict[str, str] = {}  # session_id → bot_id
_bot_to_session: dict[str, str] = {}  # bot_id → session_id

# Persistent stream state file — shared across MCP process instances
_STREAM_STATE_PATH = ACTIVITY_DIR / ".stream-state.json"


def _save_stream_state() -> None:
    """Persist stream mappings to disk for cross-process access."""
    state = {
        "bot_to_session": _bot_to_session,
        "session_to_bot": _session_to_bot,
        "active_streams": _active_streams,
        "stream_seq": _stream_seq,
    }
    _STREAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STREAM_STATE_PATH.write_text(json.dumps(state, indent=2))


def _load_stream_state() -> None:
    """Load stream mappings from disk into module-level dicts."""
    global _bot_to_session, _session_to_bot, _active_streams, _stream_seq
    if not _STREAM_STATE_PATH.exists():
        return
    try:
        state = json.loads(_STREAM_STATE_PATH.read_text())
        _bot_to_session.update(state.get("bot_to_session", {}))
        _session_to_bot.update(state.get("session_to_bot", {}))
        _active_streams.update(state.get("active_streams", {}))
        _stream_seq.update(state.get("stream_seq", {}))
    except (json.JSONDecodeError, OSError):
        pass  # Corrupted or locked — skip, in-memory state still works

BOT_STATUS_MESSAGES = {
    "joining_call": "Bot is joining the meeting...",
    "in_waiting_room": "Bot is in the waiting room. The host needs to admit it.",
    "in_call_not_recording": "Bot is in the call but not yet recording. It may need recording permission.",
    "recording_permission_allowed": "Bot has recording permission and is starting to record.",
    "in_call_recording": "Bot is recording the meeting.",
    "call_ended": "Meeting has ended. Processing recording...",
    "done": "Meeting complete. Transcript is available — use get_transcript to retrieve it.",
    "fatal": "Bot encountered an error and could not complete recording.",
}


async def _recallai_request(method: str, path: str, json: dict | None = None) -> httpx.Response:
    """Make an authenticated request to the Recall.ai API."""
    if not RECALLAI_API_KEY:
        raise ValueError("RECALLAI_API_KEY not set. Add it to your .env file.")

    async with httpx.AsyncClient() as client:
        response = await client.request(
            method,
            f"{RECALLAI_BASE_URL}{path}",
            headers={"Authorization": f"Token {RECALLAI_API_KEY}"},
            json=json,
            timeout=30.0,
        )
        response.raise_for_status()
        return response


def _format_transcript(segments: list[dict]) -> str:
    """Format raw Recall.ai transcript segments into readable speaker-attributed text."""
    parts = []
    for segment in segments:
        participant = segment.get("participant", {})
        name = participant.get("name") or "Unknown Speaker"
        words = segment.get("words", [])
        text = " ".join(w.get("text", "").strip() for w in words if w.get("text", "").strip())
        if text:
            parts.append(f"{name}: {text}")
    return "\n\n".join(parts)


# --- WebSocket streaming helpers ---


def _parse_ts(ts) -> datetime:
    """Parse a timestamp that may be ISO string or Unix float."""
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()


def _get_jsonl_path(session_id: str, project: str = "") -> Path:
    """Get the JSONL file path for a streaming session."""
    effective_project = project or DEFAULT_PROJECT
    return ACTIVITY_DIR / effective_project / f"live-transcript-{session_id}.jsonl"


async def _ws_handler(websocket: websockets.ServerConnection) -> None:
    """Handle an incoming WebSocket connection from Recall.ai."""
    # Extract session_id from path: /transcript/{session_id}
    path = urlparse(websocket.request.path)
    path_parts = path.path.strip("/").split("/")
    if len(path_parts) < 2 or path_parts[0] != "transcript":
        logger.warning("WS connection rejected: invalid path %s", path.path)
        await websocket.close(4000, "Invalid path")
        return

    session_id = path_parts[1]

    # Validate token from query params
    query = parse_qs(path.query)
    token = query.get("token", [None])[0]
    if token != EGREGORE_WS_TOKEN:
        logger.warning("WS connection rejected: invalid token for session %s", session_id)
        await websocket.close(4001, "Invalid token")
        return

    logger.info("WS connection accepted for session %s", session_id)

    stream_info = _active_streams.get(session_id)
    if not stream_info:
        logger.warning("WS connection for unknown session %s — accepting anyway", session_id)
        jsonl_path = _get_jsonl_path(session_id)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _active_streams[session_id] = {
            "bot_id": session_id,
            "chunk_count": 0,
            "last_ts": None,
            "jsonl_path": str(jsonl_path),
        }
        _stream_seq[session_id] = 0
        _save_stream_state()
        stream_info = _active_streams[session_id]

    jsonl_path = Path(stream_info["jsonl_path"])

    try:
        async for raw_message in websocket:
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.warning("WS received non-JSON message, skipping")
                continue

            event_type = msg.get("event")
            if event_type != "transcript.data":
                logger.debug("WS ignoring event type: %s", event_type)
                continue

            # Parse transcript data — handle both data.data.words and data.words
            inner = msg.get("data", {})
            if "data" in inner:
                inner = inner["data"]

            words = inner.get("words", [])
            participant = inner.get("participant", {})
            speaker = participant.get("name") or "Unknown Speaker"
            speaker_id = participant.get("id", 0)

            text = " ".join(w.get("text", "").strip() for w in words if w.get("text", "").strip())
            if not text:
                continue

            # Get timestamp from first word
            ts = None
            if words:
                ts_info = words[0].get("start_timestamp", {})
                ts = ts_info.get("absolute") or ts_info.get("relative", 0.0)

            seq = _stream_seq.get(session_id, 0)
            _stream_seq[session_id] = seq + 1

            line = {
                "ts": ts,
                "speaker": speaker,
                "speaker_id": speaker_id,
                "text": text,
                "seq": seq,
            }

            with open(jsonl_path, "a") as f:
                f.write(json.dumps(line) + "\n")

            stream_info["chunk_count"] = stream_info.get("chunk_count", 0) + 1
            stream_info["last_ts"] = ts
            logger.debug("WS wrote chunk seq=%d for session %s", seq, session_id)

    except websockets.ConnectionClosed:
        logger.info("WS connection closed for session %s", session_id)
    except Exception:
        logger.exception("WS handler error for session %s", session_id)


async def _start_ws_server() -> None:
    """Start the WebSocket server if not already running."""
    global _ws_server
    if _ws_server is not None:
        return

    _ws_server = await websockets.serve(
        _ws_handler,
        "0.0.0.0",
        EGREGORE_WS_PORT,
    )
    logger.info("WebSocket server started on port %d", EGREGORE_WS_PORT)


def _build_streaming_config(session_id: str) -> dict:
    """Build a recording config with realtime streaming endpoints."""
    ws_url = f"wss://{urlparse(EGREGORE_TUNNEL_URL).hostname}/transcript/{session_id}?token={EGREGORE_WS_TOKEN}"

    config = json.loads(json.dumps(RECALLAI_RECORDING_CONFIG))  # deep copy
    config["transcript"]["provider"]["recallai_streaming"]["mode"] = "prioritize_low_latency"
    config["transcript"]["provider"]["recallai_streaming"]["language_code"] = "en"  # required for low latency mode
    config["realtime_endpoints"] = [
        {
            "type": "websocket",
            "url": ws_url,
            "events": ["transcript.data"],
        }
    ]
    return config


mcp = FastMCP("egregore")


def _discover_session_file(project: str) -> Path | None:
    """Find the most recently modified session file for today in the project directory."""
    project_dir = ACTIVITY_DIR / project
    if not project_dir.exists():
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    session_files = sorted(
        project_dir.glob(f"{today}-*-session.md"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return session_files[0] if session_files else None


def _create_session_file(project: str) -> Path:
    """Create a new session log file with the standard template."""
    project_dir = ACTIVITY_DIR / project
    project_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    filename = f"{now.strftime('%Y-%m-%d-%H%M')}-session.md"
    filepath = project_dir / filename

    header = f"# Session - {now.strftime('%Y-%m-%d %H:%M')} ({project})\n\n"
    start_entry = f"[{now.strftime('%Y-%m-%d %H:%M')}] Start\n"
    filepath.write_text(header + start_entry)

    logger.info("Created session file: %s", filepath)
    return filepath


def _resolve_session_file(project: str, session_file: str) -> Path:
    """Resolve the session file path from explicit path or discovery."""
    if session_file:
        return Path(session_file)

    effective_project = project or DEFAULT_PROJECT
    found = _discover_session_file(effective_project)
    if found:
        return found

    return _create_session_file(effective_project)


def _add_timestamp(entry: str) -> str:
    """Add timestamp prefix if the entry doesn't already have one."""
    if TIMESTAMP_RE.match(entry.strip()):
        return entry
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"[{now}] {entry}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Append Session Log Entry",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    )
)
async def session_log_append(
    entries: str,
    project: str = "",
    session_file: str = "",
) -> str:
    """Append one or more timestamped entries to the current session's log file.

    Each entry should include a [CATEGORY] prefix (e.g., [TASK], [DEC], [RESEARCH]).
    Timestamp is auto-prepended if not present. Separate multiple entries with newlines.

    Args:
        entries: One or more log entries, newline-separated.
        project: Project name (e.g., "village"). Defaults to EGREGORE_DEFAULT_PROJECT env var.
        session_file: Explicit path to session log file. Bypasses discovery.
    """
    filepath = _resolve_session_file(project, session_file)

    lines = [line for line in entries.strip().split("\n") if line.strip()]
    if not lines:
        return "No entries to append."

    timestamped = [_add_timestamp(line) for line in lines]
    content = "\n".join(timestamped) + "\n"

    with open(filepath, "a") as f:
        f.write(content)

    logger.info("Appended %d entries to %s", len(timestamped), filepath)
    return f"Appended {len(timestamped)} entry(ies) to {filepath}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read Recent Session Log",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    )
)
async def session_log_read_recent(
    lines: int = 10,
    project: str = "",
    session_file: str = "",
) -> str:
    """Read the most recent entries from the current session's log file.

    Args:
        lines: Number of recent lines to return (default: 10).
        project: Project name (e.g., "village"). Defaults to EGREGORE_DEFAULT_PROJECT env var.
        session_file: Explicit path to session log file. Bypasses discovery.
    """
    if session_file:
        filepath = Path(session_file)
    else:
        effective_project = project or DEFAULT_PROJECT
        filepath = _discover_session_file(effective_project)

    if filepath is None or not filepath.exists():
        return (
            "No session log file found. "
            "Use /session-log to create one, or start a new session."
        )

    all_lines = filepath.read_text().splitlines()
    recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return "\n".join(recent)


# --- Recall.ai meeting bot tools ---


@mcp.tool(
    annotations=ToolAnnotations(
        title="Observe Meeting",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def observe_meeting(
    meeting_url: str,
    bot_name: str = "Echobot (Matt)",
    streaming: bool = False,
    project: str = "",
) -> str:
    """Dispatch a Recall.ai bot to observe and transcribe a meeting.

    Sends a bot to join the meeting URL. The bot records with accuracy-optimized
    transcription and perfect diarization. It auto-leaves when everyone else leaves.

    When streaming=True, enables real-time transcript streaming via WebSocket.
    Requires EGREGORE_TUNNEL_URL to be set (ngrok stable domain) and ngrok running.
    Use get_live_chunks to poll for new transcript data during the meeting.

    Args:
        meeting_url: The meeting URL (Zoom, Google Meet, etc.)
        bot_name: Display name for the bot in the meeting (default: "EchoBot")
        streaming: Enable real-time transcript streaming (default: False)
        project: Project name for JSONL file location (default: EGREGORE_DEFAULT_PROJECT)
    """
    try:
        if streaming:
            if not EGREGORE_TUNNEL_URL:
                return "Error: EGREGORE_TUNNEL_URL not set. Add it to your .env (e.g., https://mhull.ngrok.dev)."

            session_id = str(uuid.uuid4())
            await _start_ws_server()
            recording_config = _build_streaming_config(session_id)

            # Prepare JSONL path
            jsonl_path = _get_jsonl_path(session_id, project)
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            recording_config = RECALLAI_RECORDING_CONFIG

        response = await _recallai_request("POST", "/bot/", json={
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "recording_config": recording_config,
        })
        data = response.json()
        bot_id = data.get("id", "unknown")

        if streaming:
            # Register stream mappings
            _session_to_bot[session_id] = bot_id
            _bot_to_session[bot_id] = session_id
            _stream_seq[session_id] = 0
            _active_streams[session_id] = {
                "bot_id": bot_id,
                "chunk_count": 0,
                "last_ts": None,
                "jsonl_path": str(jsonl_path),
            }
            _save_stream_state()
            return (
                f"Bot dispatched with live streaming. Bot ID: {bot_id}\n\n"
                f"Streaming session: {session_id}\n"
                f"Ensure ngrok is running: ngrok http {EGREGORE_WS_PORT} --domain={urlparse(EGREGORE_TUNNEL_URL).hostname}\n\n"
                f"Use get_live_chunks(bot_id=\"{bot_id}\") to poll for transcript data."
            )

        return f"Bot dispatched to meeting. Bot ID: {bot_id}\n\nUse check_bot with this ID to monitor status, or get_transcript once the meeting ends."
    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        return f"Recall.ai API error ({e.response.status_code}): {e.response.text}"
    except httpx.HTTPError as e:
        return f"Failed to reach Recall.ai API: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Check Bot Status",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def check_bot(bot_id: str) -> str:
    """Check the current status of a Recall.ai meeting bot.

    Args:
        bot_id: The bot ID returned by observe_meeting.
    """
    try:
        response = await _recallai_request("GET", f"/bot/{bot_id}/")
        data = response.json()
        status_changes = data.get("status_changes", [])
        raw_status = status_changes[-1]["code"] if status_changes else "unknown"
        message = BOT_STATUS_MESSAGES.get(raw_status, f"Bot status: {raw_status}")
        result = f"Bot {bot_id}: {message}"

        # Append streaming status if applicable
        _load_stream_state()
        session_id = _bot_to_session.get(bot_id)
        if session_id and session_id in _active_streams:
            info = _active_streams[session_id]
            chunk_count = info.get("chunk_count", 0)
            last_ts = info.get("last_ts")
            terminal_states = {"call_ended", "done", "fatal"}
            if raw_status in terminal_states:
                result += f"\nStreaming: Complete ({chunk_count} total chunks)"
            else:
                ts_str = ""
                if last_ts:
                    dt = _parse_ts(last_ts)
                    ts_str = f", last: {dt.strftime('%H:%M:%S')}"
                result += f"\nStreaming: Active ({chunk_count} chunks received{ts_str})"

        return result
    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Bot {bot_id} not found. Check the bot ID and try again."
        return f"Recall.ai API error ({e.response.status_code}): {e.response.text}"
    except httpx.HTTPError as e:
        return f"Failed to reach Recall.ai API: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Meeting Transcript",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def get_transcript(bot_id: str) -> str:
    """Retrieve the transcript for a completed meeting.

    Returns a formatted, speaker-attributed transcript if the meeting is done
    and the transcript is ready. Otherwise returns a status message.

    Args:
        bot_id: The bot ID returned by observe_meeting.
    """
    try:
        response = await _recallai_request("GET", f"/bot/{bot_id}/")
        data = response.json()
        status_changes = data.get("status_changes", [])
        raw_status = status_changes[-1]["code"] if status_changes else "unknown"

        if raw_status != "done":
            message = BOT_STATUS_MESSAGES.get(raw_status, f"Bot status: {raw_status}")
            return f"Transcript not ready. {message}"

        # Check transcript availability — in recordings array, not top-level
        recordings = data.get("recordings", [])
        transcript_obj = recordings[0].get("media_shortcuts", {}).get("transcript", {}) if recordings else {}
        transcript_status = transcript_obj.get("status", {}).get("code")
        download_url = transcript_obj.get("data", {}).get("download_url")

        if transcript_status != "done" or not download_url:
            return "Meeting is complete but the transcript is still processing. Try again in a moment."

        # Download and format the transcript
        async with httpx.AsyncClient() as client:
            dl_response = await client.get(download_url, timeout=30.0)
            dl_response.raise_for_status()
            segments = dl_response.json()

        formatted = _format_transcript(segments)
        if not formatted:
            return "Meeting is complete but the transcript is empty (no speech detected)."

        # Write to /tmp and return path (avoids bloating LLM context with full transcript)
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        out_path = Path(f"/tmp/{timestamp}-meeting-{bot_id[:8]}.md")
        out_path.write_text(formatted)
        return f"Transcript saved to {out_path} ({len(segments)} segments, {len(formatted)} chars)"

    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Bot {bot_id} not found. Check the bot ID and try again."
        return f"Recall.ai API error ({e.response.status_code}): {e.response.text}"
    except httpx.HTTPError as e:
        return f"Failed to reach Recall.ai API: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Live Transcript Chunks",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    )
)
async def get_live_chunks(
    bot_id: str,
    project: str = "",
) -> str:
    """Get new transcript chunks since last call for an active streaming session.

    Returns incrementally — only chunks not yet seen. Call repeatedly to poll
    for new transcript data during a live meeting.

    Args:
        bot_id: The bot ID returned by observe_meeting (with streaming=True).
        project: Project name (default: EGREGORE_DEFAULT_PROJECT).
    """
    # Load persisted state from disk (may have been set by another process)
    _load_stream_state()

    # Resolve session_id from bot_id
    session_id = _bot_to_session.get(bot_id)

    # Try to find JSONL file by bot_id-based session or direct bot_id
    jsonl_path = None
    if session_id:
        stream_info = _active_streams.get(session_id)
        if stream_info:
            jsonl_path = Path(stream_info["jsonl_path"])

    if jsonl_path is None:
        # Fallback: try to find a JSONL file matching bot_id directly
        effective_project = project or DEFAULT_PROJECT
        candidate = ACTIVITY_DIR / effective_project / f"live-transcript-{bot_id}.jsonl"
        if candidate.exists():
            jsonl_path = candidate
            session_id = bot_id  # use bot_id as session_id for cursor tracking

    if jsonl_path is None or not jsonl_path.exists():
        return f"No streaming session found for bot {bot_id}. Use observe_meeting with streaming=True first."

    # Read all lines and filter by cursor
    cursor_key = bot_id
    last_seen = _live_cursors.get(cursor_key, -1)

    lines = jsonl_path.read_text().strip().split("\n")
    if not lines or lines == [""]:
        return "Streaming session exists but no transcript data yet."

    new_chunks = []
    max_seq = last_seen
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        seq = entry.get("seq", -1)
        if seq > last_seen:
            new_chunks.append(entry)
            if seq > max_seq:
                max_seq = seq

    if not new_chunks:
        return "No new transcript data since last check."

    # Update cursor
    _live_cursors[cursor_key] = max_seq

    # Format output
    formatted_lines = []
    for chunk in new_chunks:
        ts = chunk.get("ts")
        if ts:
            time_str = _parse_ts(ts).strftime("%H:%M:%S")
        else:
            time_str = "??:??:??"
        speaker = chunk.get("speaker", "Unknown")
        text = chunk.get("text", "")
        formatted_lines.append(f"[{time_str}] {speaker}: {text}")

    # Summary line
    first_ts = new_chunks[0].get("ts")
    last_ts = new_chunks[-1].get("ts")
    if first_ts and last_ts:
        t1 = _parse_ts(first_ts).strftime("%H:%M:%S")
        t2 = _parse_ts(last_ts).strftime("%H:%M:%S")
        summary = f"--- {len(new_chunks)} new chunks ({t1} – {t2}) ---"
    else:
        summary = f"--- {len(new_chunks)} new chunks ---"

    return "\n".join(formatted_lines) + f"\n\n{summary}"


# --- Slack tools ---


@mcp.tool(
    annotations=ToolAnnotations(
        title="Check Slack Tags",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def check_slack_tags() -> str:
    """Check for Slack threads tagged with the attention emoji (e.g. :robot_face:).

    Reads the tagged-threads JSONL file, reduces to current state (last action per
    thread wins), and returns active tagged threads with a preview of the first message
    and reply count.

    Use this to discover which Slack threads need your attention.
    """
    if not SLACK_TAGGED_THREADS_FILE.exists():
        return "No tagged threads file found. The slack-mcp-tagger service may not be running."

    content = SLACK_TAGGED_THREADS_FILE.read_text().strip()
    if not content:
        return "No tagged threads."

    # Reduce: last action per channel+thread_ts wins
    state: dict[str, dict] = {}
    for line in content.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = f"{entry.get('channel')}:{entry.get('thread_ts')}"
        state[key] = entry

    # Filter to active tags only
    active = {k: v for k, v in state.items() if v.get("action") == "tag"}
    if not active:
        return "No active tagged threads."

    # Fetch preview for each tagged thread
    client = _get_slack_client()
    results = []
    for key, tag in active.items():
        channel = tag["channel"]
        thread_ts = tag["thread_ts"]
        tagged_at = tag.get("tagged_at", "unknown")

        try:
            # Get the parent message
            resp = await client.conversations_history(
                channel=channel, latest=thread_ts, inclusive=True, limit=1
            )
            messages = resp.get("messages", [])
            if messages:
                msg = messages[0]
                preview = msg.get("text", "")[:200]
                reply_count = msg.get("reply_count", 0)
                user = msg.get("user", "unknown")
            else:
                preview = "(could not fetch message)"
                reply_count = 0
                user = "unknown"

            # Try to resolve channel name
            try:
                ch_resp = await client.conversations_info(channel=channel)
                ch_name = "#" + ch_resp["channel"]["name"]
            except Exception:
                ch_name = channel

            results.append(
                f"- channel: {ch_name} ({channel})\n"
                f"  thread_ts: {thread_ts}\n"
                f"  tagged_at: {tagged_at}\n"
                f"  author: {user}\n"
                f"  preview: {preview}\n"
                f"  replies: {reply_count}"
            )
        except SlackApiError as e:
            results.append(f"- channel: {channel}, thread_ts: {thread_ts} — error: {e.response['error']}")

    return "TAGGED_THREADS:\n" + "\n".join(results)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read Slack Thread",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def read_slack_thread(channel: str, thread_ts: str) -> str:
    """Read all messages in a Slack thread.

    Returns a formatted conversation with author names, timestamps, and message text.

    Args:
        channel: Channel name (e.g. '#agentic-pilot-ops') or channel ID (e.g. 'C0AEVCZN7JT').
        thread_ts: Thread timestamp (the ts of the parent message).
    """
    channel_id, channel_name = await _resolve_slack_channel(channel)
    if channel_name is None:
        return f"Could not resolve channel '{channel}'. Check the name or ID."

    client = _get_slack_client()
    try:
        resp = await client.conversations_replies(channel=channel_id, ts=thread_ts, limit=100)
        messages = resp.get("messages", [])
        if not messages:
            return "Thread not found or empty."

        # Build user cache for display names
        user_cache: dict[str, str] = {}

        async def resolve_user(uid: str) -> str:
            if uid in user_cache:
                return user_cache[uid]
            try:
                u = await client.users_info(user=uid)
                name = u["user"].get("real_name") or u["user"].get("name") or uid
            except Exception:
                name = uid
            user_cache[uid] = name
            return name

        formatted = []
        for msg in messages:
            user = await resolve_user(msg.get("user", "unknown"))
            ts = float(msg.get("ts", 0))
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
            time_str = dt.strftime("%Y-%m-%d %H:%M")
            text = msg.get("text", "")
            formatted.append(f"[{time_str}] {user}: {text}")

        return "\n\n".join(formatted)
    except SlackApiError as e:
        return f"Slack API error: {e.response['error']}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Post to Slack",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def post_to_slack(channel: str, text: str) -> str:
    """Post a new message to a Slack channel. Channel must be in the write whitelist.

    Use this to start a new conversation (not reply to an existing thread).
    Link unfurling is disabled for security (prevents data exfiltration via previews).

    Args:
        channel: Channel name (e.g. '#agentic-pilot-ops') or channel ID (e.g. 'C0AEVCZN7JT').
        text: Message text to post.
    """
    channel_id, channel_name = await _resolve_slack_channel(channel)

    if channel_name is None:
        return f"Could not resolve channel '{channel}'. Check the name or ID."

    if channel_id not in SLACK_WRITE_CHANNELS:
        allowed = await _resolve_whitelist_names()
        return f"#{channel_name} is not in the write whitelist. Allowed: {', '.join(allowed)}"

    client = _get_slack_bot_client()
    try:
        resp = await client.chat_postMessage(
            channel=channel_id,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )
        ts = resp.get("ts", "unknown")
        return f"Message posted to #{channel_name} (ts: {ts})"
    except SlackApiError as e:
        return f"Slack API error: {e.response['error']}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Reply to Slack Thread",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def reply_to_slack_thread(channel: str, thread_ts: str, text: str) -> str:
    """Post a reply to a Slack thread. Channel must be in the write whitelist.

    Link unfurling is disabled for security (prevents data exfiltration via previews).

    Args:
        channel: Channel name (e.g. '#agentic-pilot-ops') or channel ID (e.g. 'C0AEVCZN7JT').
        thread_ts: Thread timestamp to reply to.
        text: Message text to post.
    """
    channel_id, channel_name = await _resolve_slack_channel(channel)

    if channel_name is None:
        return f"Could not resolve channel '{channel}'. Check the name or ID."

    if channel_id not in SLACK_WRITE_CHANNELS:
        allowed = await _resolve_whitelist_names()
        return f"#{channel_name} is not in the write whitelist. Allowed: {', '.join(allowed)}"

    client = _get_slack_bot_client()
    try:
        resp = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )
        ts = resp.get("ts", "unknown")
        return f"Reply posted to #{channel_name} (ts: {ts})"
    except SlackApiError as e:
        return f"Slack API error: {e.response['error']}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add Slack Reaction",
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def add_reaction(channel: str, message_ts: str, emoji: str) -> str:
    """Add an emoji reaction to a Slack message.

    Args:
        channel: Channel name (e.g. '#superteam') or channel ID.
        message_ts: Timestamp of the message to react to (from scan_channel output).
        emoji: Emoji name without colons (e.g. 'bookmark', 'white_check_mark', 'eyes').
    """
    channel_id, channel_name = await _resolve_slack_channel(channel)
    if channel_name is None:
        return f"Could not resolve channel '{channel}'. Check the name or ID."

    emoji_clean = emoji.strip(":")

    client = _get_slack_bot_client()
    try:
        await client.reactions_add(
            channel=channel_id,
            timestamp=message_ts,
            name=emoji_clean,
        )
        return f"Added :{emoji_clean}: to message in #{channel_name} (as Nuncius)"
    except SlackApiError as e:
        return f"Slack API error: {e.response['error']}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Remove Slack Reaction",
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=True,
    )
)
async def remove_reaction(channel: str, message_ts: str, emoji: str) -> str:
    """Remove an emoji reaction from a Slack message.

    Args:
        channel: Channel name (e.g. '#superteam') or channel ID.
        message_ts: Timestamp of the message to remove the reaction from.
        emoji: Emoji name without colons (e.g. 'bookmark', 'white_check_mark', 'eyes').
    """
    channel_id, channel_name = await _resolve_slack_channel(channel)
    if channel_name is None:
        return f"Could not resolve channel '{channel}'. Check the name or ID."

    emoji_clean = emoji.strip(":")

    client = _get_slack_bot_client()
    try:
        await client.reactions_remove(
            channel=channel_id,
            timestamp=message_ts,
            name=emoji_clean,
        )
        return f"Removed :{emoji_clean}: from message in #{channel_name} (as Nuncius)"
    except SlackApiError as e:
        return f"Slack API error: {e.response['error']}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Scan Slack Channel",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def scan_channel(channel: str, hours_back: float = 24, limit: int = 200, emoji: str = "") -> str:
    """Scan recent messages in a Slack channel.

    Fetches messages going back approximately `hours_back` hours. Paginates
    automatically, stopping when messages are older than the cutoff or `limit`
    is reached.

    Args:
        channel: Channel name (e.g. '#product') or channel ID.
        hours_back: How many hours back to scan (default 24).
        limit: Maximum number of messages to return (default 200).
        emoji: If set, only return messages that have this reaction (e.g. 'robot_face', 'eyes').
    """
    channel_id, channel_name = await _resolve_slack_channel(channel)
    if channel_name is None:
        return f"Could not resolve channel '{channel}'. Check the name or ID."

    client = _get_slack_client()
    cutoff = time.time() - (hours_back * 3600)

    user_cache: dict[str, str] = {}

    async def resolve_user(uid: str) -> str:
        if uid in user_cache:
            return user_cache[uid]
        try:
            u = await client.users_info(user=uid)
            name = u["user"].get("real_name") or u["user"].get("name") or uid
        except Exception:
            name = uid
        user_cache[uid] = name
        return name

    collected = []
    cursor = None
    hit_cutoff = False

    try:
        while len(collected) < limit and not hit_cutoff:
            kwargs: dict = {"channel": channel_id, "limit": min(100, limit - len(collected))}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await client.conversations_history(**kwargs)
            messages = resp.get("messages", [])
            if not messages:
                break

            for msg in messages:
                ts = float(msg.get("ts", 0))
                if ts < cutoff:
                    hit_cutoff = True
                    break

                # Filter by emoji if requested
                if emoji:
                    reactions = msg.get("reactions", [])
                    emoji_clean = emoji.strip(":")
                    if not any(r.get("name") == emoji_clean for r in reactions):
                        continue

                user = await resolve_user(msg.get("user", "unknown"))
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
                time_str = dt.strftime("%Y-%m-%d %H:%M")
                text = msg.get("text", "")
                reply_count = msg.get("reply_count", 0)
                thread_ts = msg.get("thread_ts", "")

                line = f"[{time_str}] {user}: {text}"
                if reply_count > 0:
                    line += f"\n  [{reply_count} replies, thread_ts: {thread_ts}]"
                collected.append(line)

                if len(collected) >= limit:
                    break

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except SlackApiError as e:
        return f"Slack API error: {e.response['error']}"

    emoji_suffix = f" with :{emoji.strip(':')}:" if emoji else ""
    if not collected:
        return f"No messages{emoji_suffix} in #{channel_name} in the last {hours_back}h."

    header = f"#{channel_name} — {len(collected)} messages{emoji_suffix} (last {hours_back}h)"
    return header + "\n\n" + "\n\n".join(collected)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search Slack",
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=True,
    )
)
async def search_slack(query: str, count: int = 10) -> str:
    """Search Slack messages. Uses Slack search syntax (supports from:, in:, before:, after:, etc.).

    Returns compressed results: channel, author, timestamp, and message snippet for each match.
    Requires a user OAuth token (xoxp) — bot tokens cannot search.

    Args:
        query: Search query using Slack search syntax.
        count: Number of results to return (default: 10, max: 50).
    """
    client = _get_slack_client()
    count = min(count, 50)
    try:
        resp = await client.search_messages(query=query, count=count, sort="timestamp", sort_dir="desc")
        matches = resp.get("messages", {}).get("matches", [])
        if not matches:
            return f"No results for: {query}"

        total = resp.get("messages", {}).get("total", 0)
        results = []
        for m in matches:
            channel_name = m.get("channel", {}).get("name", "unknown")
            user = m.get("username", "unknown")
            ts = float(m.get("ts", 0))
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
            time_str = dt.strftime("%Y-%m-%d %H:%M")
            text = m.get("text", "")[:200]
            permalink = m.get("permalink", "")
            results.append(f"- [{time_str}] #{channel_name} | {user}: {text}\n  {permalink}")

        header = f"Found {total} results (showing {len(matches)}):\n"
        return header + "\n".join(results)
    except SlackApiError as e:
        return f"Slack API error: {e.response['error']}"


async def _main() -> None:
    await mcp.run_stdio_async()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
