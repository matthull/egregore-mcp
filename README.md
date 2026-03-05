# egregore-mcp

Unified MCP server for egregore tools — session logging, Recall.ai meeting bots, and Slack integration.

## Setup

```bash
pip install -e .
```

Create a `.env` file (see configuration below), then run:

```bash
egregore-mcp
```

## Claude Code Integration

egregore-mcp is configured as an MCP server in Claude Code's global config (`~/.claude.json`):

```
Command: uv
Args: --directory /home/matt/code/egregore-mcp run server.py
```

This launches the server via `uv` directly from the source directory — no install step required.

## Configuration

All configuration is via environment variables (loaded from `.env`).

### Session Logging

| Variable | Default | Description |
|---|---|---|
| `EGREGORE_ACTIVITY_DIR` | `~/egregore/activity` | Root directory for session log files |
| `EGREGORE_DEFAULT_PROJECT` | `village` | Default project name for log paths |

### Recall.ai (Meeting Bots)

| Variable | Default | Description |
|---|---|---|
| `RECALLAI_API_KEY` | _(required)_ | Recall.ai API key |
| `RECALLAI_REGION` | `us-west-2` | Recall.ai region |

### Streaming (WebSocket)

| Variable | Default | Description |
|---|---|---|
| `EGREGORE_WS_PORT` | `8765` | Local WebSocket server port for live transcript streaming |
| `EGREGORE_WS_TOKEN` | _(auto-generated)_ | Auth token for WebSocket connections; random if not set |
| `EGREGORE_TUNNEL_URL` | _(empty)_ | Public tunnel URL for Recall.ai real-time webhooks |

### Slack

| Variable | Default | Description |
|---|---|---|
| `SLACK_USER_TOKEN` | _(required)_ | Slack user token (`xoxp-...`) for read/search operations |
| `SLACK_BOT_TOKEN` | _(required)_ | Slack bot token (`xoxb-...`) for write operations (posting, reactions) |
| `SLACK_WRITE_CHANNELS` | _(empty)_ | **Write allowlist** — comma-separated channel IDs (see below) |
| `SLACK_TAG_EMOJI` | `robot_face` | Emoji used to tag triaged threads |
| `SLACK_TAGGED_THREADS_FILE` | `~/.slack-tagged-threads.jsonl` | Persistent store for already-tagged thread timestamps |

## Slack Channel Write Allowlist

Write operations (`post_to_slack`, `reply_to_slack_thread`) are restricted to channels explicitly listed in `SLACK_WRITE_CHANNELS`. This is a safety mechanism — the MCP server will refuse to post to any channel not on the list.

**Read operations are not restricted** — `scan_channel`, `read_slack_thread`, `search_slack`, and `check_slack_tags` work on any channel the user token has access to.

### How it works

1. Set `SLACK_WRITE_CHANNELS` to a comma-separated list of **channel IDs** (not names):

   ```env
   SLACK_WRITE_CHANNELS=C0AEVCZN7JT,C07UXBF3K5L
   ```

2. To find a channel ID, use one of these methods:

   **Slack UI:** Open the channel, click the channel name in the header. The channel ID is at the bottom of the "About" modal (starts with `C`).

   **Slack API:** List all channels and filter by name:
   ```bash
   curl -s -H "Authorization: Bearer $SLACK_USER_TOKEN" \
     "https://slack.com/api/conversations.list?types=public_channel&limit=999" \
     | python3 -c "import sys,json; [print(c['id'], c['name']) for c in json.load(sys.stdin)['channels']]"
   ```

3. If a write is attempted to a channel not on the list, the tool returns an error listing the allowed channels by name.

### Why channel IDs?

Channel names can be renamed; IDs are stable. Using IDs ensures the allowlist doesn't silently break when someone renames a channel.

## Tools

### Session Logging
- **session_log_append** — Append timestamped entries to a session log file
- **session_log_read_recent** — Read recent entries from a session log

### Meeting Bots (Recall.ai)
- **observe_meeting** — Send a bot to a meeting URL to record/transcribe
- **check_bot** — Check the status of a meeting bot
- **get_transcript** — Retrieve the full transcript from a completed bot
- **get_live_chunks** — Stream live transcript chunks via WebSocket

### Slack
- **scan_channel** — Scan recent messages in a channel
- **read_slack_thread** — Read all messages in a thread
- **search_slack** — Full-text search across Slack
- **check_slack_tags** — Check for untagged threads needing triage
- **post_to_slack** — Post a message (write-allowlisted)
- **reply_to_slack_thread** — Reply to a thread (write-allowlisted)
- **add_reaction** — Add an emoji reaction to a message
