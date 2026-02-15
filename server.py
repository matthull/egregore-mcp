"""egregore-mcp: Unified MCP server for egregore tools.

Features:
- Session-log tools for zero-friction session logging
- Recall.ai meeting bot tools for dispatching bots and retrieving transcripts
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
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

BOT_STATUS_MESSAGES = {
    "bot.joining_call": "Bot is joining the meeting...",
    "bot.in_waiting_room": "Bot is in the waiting room. The host needs to admit it.",
    "bot.in_call_not_recording": "Bot is in the call but not yet recording. It may need recording permission.",
    "bot.recording_permission_allowed": "Bot has recording permission and is starting to record.",
    "bot.in_call_recording": "Bot is recording the meeting.",
    "bot.call_ended": "Meeting has ended. Processing recording...",
    "bot.done": "Meeting complete. Transcript is available — use get_transcript to retrieve it.",
    "bot.fatal": "Bot encountered an error and could not complete recording.",
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
    bot_name: str = "Meeting Notetaker",
) -> str:
    """Dispatch a Recall.ai bot to observe and transcribe a meeting.

    Sends a bot to join the meeting URL. The bot records with accuracy-optimized
    transcription and perfect diarization. It auto-leaves when everyone else leaves.

    Args:
        meeting_url: The meeting URL (Zoom, Google Meet, etc.)
        bot_name: Display name for the bot in the meeting (default: "Meeting Notetaker")
    """
    try:
        response = await _recallai_request("POST", "/bot/", json={
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "recording_config": RECALLAI_RECORDING_CONFIG,
        })
        data = response.json()
        bot_id = data.get("id", "unknown")
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
        raw_status = data.get("status", "unknown")
        message = BOT_STATUS_MESSAGES.get(raw_status, f"Bot status: {raw_status}")
        return f"Bot {bot_id}: {message}"
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
        raw_status = data.get("status", "unknown")

        if raw_status != "bot.done":
            message = BOT_STATUS_MESSAGES.get(raw_status, f"Bot status: {raw_status}")
            return f"Transcript not ready. {message}"

        # Check transcript availability
        transcript_data = data.get("media_shortcuts", {}).get("transcript", {}).get("data", {})
        transcript_status = transcript_data.get("status")
        download_url = transcript_data.get("download_url")

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

        return formatted

    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Bot {bot_id} not found. Check the bot ID and try again."
        return f"Recall.ai API error ({e.response.status_code}): {e.response.text}"
    except httpx.HTTPError as e:
        return f"Failed to reach Recall.ai API: {e}"


async def _main() -> None:
    await mcp.run_stdio_async()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
