# Architecture

> **Version guard**: This document reflects the `HEAD` at **2.0.0** (2026-05-09).
> If the code has moved on, this may be stale. Run `git log ARCHITECTURE.md` to check
> whether it's been updated for the current HEAD, and update it if you make structural
> changes.

This document describes the architecture of `claude-code-discord-bot-hook`, a Claude Code plugin that bridges Claude Code CLI sessions to Discord. It serves as a map for anyone working on or extending the codebase.

## Project Overview

This is a **bidirectional monitor and remote control handler** for Claude Code. When Claude Code fires hook events during a session, the plugin sends rich interactive messages to a Discord channel. A user can approve or deny Claude's actions (e.g., running a bash command, writing a file) from Discord — without being at the computer. Additionally, the user can sync a session's conversation to a forum post and send prompts back into the running tmux session from Discord.

Notable characteristics:

- **Bidirectional** — not just monitoring. The `/sync` command mirrors conversation to a forum post and forwards typed replies to the tmux pane via `tmux send-keys`.
- **PID-based tmux discovery** — the bot finds the correct tmux pane by walking the process ancestor tree via `ps -o ppid=`, not by checking which pane is focused. Works correctly when the user has multiple panes and walks away without focusing the Claude Code pane.
- **Session rename-safe** — thread cache is keyed by immutable `session_id`, not mutable session label. Session thread names are updated in-place when the user renames a session.
- **Two-process design**: a short-lived shim per hook event, and a long-lived bot for the Discord WebSocket connection.
- **Distributed** as a [Claude Code marketplace plugin](https://github.com/TonyWu20/my-claude-marketplace).

## Directory Structure

```
.
├── .claude-plugin/
│   └── plugin.json              # Marketplace plugin manifest
├── hooks/                       # Core hook implementation
│   ├── pyproject.toml           # Python deps (discord.py, pytest)
│   ├── uv.lock
│   ├── hooks.json               # Hook event → command mapping
│   ├── notify_discord.py        # Hook entry point shim (short-lived, per-event)
│   ├── discord_bot.py           # Persistent Discord bot (long-lived)
│   └── tests/
│       ├── simulate.py          # Test harness for hook events
│       └── fixtures/            # 9 JSON fixtures for all hook event types
├── tests/                       # Project-level pytest suite
│   ├── conftest.py
│   ├── test_discord_bot.py
│   └── test_notify_discord.py
├── cards/                       # Marketing/presentation SVGs (separate uv project)
├── cards_bauhaus/               # Alternate Bauhaus-style card variants
├── ARCHITECTURE.md              # This file
├── README.md
└── CHANGE_LOG.md
```

## Core Modules

### `notify_discord.py` — Hook Entry Point Shim

Claude Code invokes this as a subprocess for each registered hook event. It **must exit quickly** — it reads JSON from stdin, forwards the event to the persistent bot via a Unix socket, and exits.

Key responsibilities:

- **Bot lifecycle**: `ensure_bot_running()` spawns `discord_bot.py` as a detached child if not already running. When `DISCORD_BOT_REMOTE=true`, skips local spawn (remote machine manages its own bot).
- **IPC to bot**: Sends JSON over a Unix domain socket (`/tmp/claude_discord.sock`) by default. When `DISCORD_BOT_HOST` is set, connects via TCP instead. Reads back one JSON response line.
- **Event dispatch**: Routes on `hook_event_name`:
  - `Stop` / `SubagentStop` — fire-and-forget notification of the assistant's last message.
  - `PermissionRequest` — blocking approval flow: sends a rich message with buttons to Discord, polls for a decision file, prints the decision JSON to stdout.
  - Other events (e.g., `Notification`) are silently ignored.
- **Special tool handling**:
  - `AskUserQuestion` renders multi-choice questions that become Discord Select menus.
  - `ExitPlanMode` reads the current plan file from `~/.claude/plans/` and sends it as native markdown.
  - `Bash` shows the command in a code block, with chunking for long commands.
- **Idle watchdog**: When `--idle-from-stdin` is passed (via `UserPromptSubmit` hook), spawns a detached child that posts "Claude is waiting for input" after 5 minutes of inactivity.
- **Stop flag**: Checks `/tmp/claude_stop_<session>.txt` before processing approvals — allows external cancellation.
- **Tmux pane discovery**: Calls `discover_tmux_target(os.getppid())` on every hook invocation and passes `tmux_target` in all IPC messages. Walks the process tree via `ps -o ppid=` — always available on macOS/Linux, no third-party package needed. The bot caches this in sync state for later use by `send-keys`.
- **IPC enrichment**: All IPC messages carry `session_id` (stable key for thread cache) and `tmux_target` (for tmux pane targeting), both optional for backward compatibility with unmodified bots.

### `discord_bot.py` — Persistent Discord Bot

This is a long-lived `discord.py` bot that holds the Discord WebSocket connection. Started once by `notify_discord.py` when first needed.

Key responsibilities:

- **IPC socket server**: Listens on `/tmp/claude_discord.sock` (or `DISCORD_BOT_HOST` when set for TCP). Each connection processes one JSON request:
  - `{"type": "notify", ...}` — posts a text message to the session's Discord thread.
  - `{"type": "approve", ...}` — posts a message with interactive buttons, then polls a decision file and returns the result.
- **Slash commands** (via `app_commands`):
  - `/sessions` — lists recent Claude Code sessions.
  - `/history [session] [tail]` — shows recent conversation messages from a session's JSONL file, formatted in a thread.
  - `/summary [date]` — aggregates per-project token usage, model breakdown, and session time for a given UTC date; posts the report as a new forum post.
  - `/sync [session]` — sync a session to a Discord forum post for away-from-desk control. Supports Select menu for multi-pane environments.
- **Button/modal interactions**: Handles `approve`, `deny`, `suggest` (permission edits), `askq` (question answering), `plan_feedback` (ExitPlanMode rejection), and `edit_rule` (rule editing before approval).
- **Session threads**: Creates one Discord thread per Claude Code session, persisted to `/tmp/claude_discord_threads.json`. Keyed by immutable `session_id` — thread names update in-place on session rename.
- **Sync state**: Persists session sync state (forum thread ID, tmux target, sync position) to `/tmp/claude_discord_sync.json`.
- **Tmux pane discovery**: Via `discover_tmux_target_for_session()` — walks the process ancestor tree via `ps -o ppid=` and matches against `tmux list-panes -F '#{pane_pid}'`. Returns a tmux target like `"0:1.1"` for `tmux send-keys`.
- **Message forwarding**: `on_message` event handler detects user messages in synced forum threads and forwards them to the tmux pane via `tmux send-keys -t <target> <text> Enter`. Newlines in messages become `C-j` (literal line breaks in the TUI) so multiline text submits as a single prompt. If the cached `tmux_target` is empty, it attempts on-demand discovery via `discover_tmux_target_for_session()` before giving up.
- **Submission confirmation**: After forwarding, `_confirm_message_submitted()` polls the session's JSONL conversation file for growth. Adds ✅ reaction only when Claude has processed the message. Retries Enter up to 4 times if the file doesn't grow, with ⚠️ fallback if all retries fail.
- **Conversation context for approvals**: `_get_conversation_context()` reads the session JSONL to extract messages from the last user prompt onward, and sends them as separate messages before the approval message (with buttons). The approver sees the full conversation that led to the tool request — nothing is trimmed.

## Data Flow

### Notification Flow (fire-and-forget)

```
Claude Code fires hook event (e.g., Stop)
→ notify_discord.py reads JSON from stdin
→ ipc({"type": "notify", ...}) → Unix socket or TCP → discord_bot.py
→ bot.posts message to session's Discord thread
→ notify_discord.py exits
```

### Approval Flow (blocking)

```
Claude Code fires PermissionRequest
→ notify_discord.py builds rich message with tool details
→ ipc({"type": "approve", ...}) → Unix socket or TCP → discord_bot.py
→ bot creates/gets session thread, posts message with Approve/Deny buttons
→ bot polls ~/.claude/discord-decisions/<id>.json every 500ms
   └─ User clicks button in Discord → on_interaction() fires
      → writes decision JSON to file
→ bot reads and deletes decision file, returns result via IPC
→ notify_discord.py prints decision to stdout, exits
```

### Idle Watchdog Flow

```
User submits prompt → UserPromptSubmit hook fires
→ notify_discord.py --idle-from-stdin
→ kills previous watchdog PID
→ spawns detached child: notify_discord.py --idle <session_label>
→ exits immediately (async hook)
   └─ detached child: sleep 300s → ipc({"type": "notify", "idle": true}) → exits
```

### Sync Flow (bidirectional control)

```
┌─ User types prompt in forum thread
│
│  discord_bot.py (on_message)
│   │  detects message in synced forum thread
│   │  looks up tmux_target from _session_sync
│   │  if empty: calls discover_tmux_target_for_session() as fallback
│   ▼
│  subprocess.run(["tmux", "send-keys", "-t", "0:1.1", "prompt", "Enter"])
│        │  (newlines → C-j for literal line breaks in TUI)
│        ▼
│  _confirm_message_submitted()        ← background async task
│   │  polls session JSONL file for growth
│   │  retries Enter up to 4× if file doesn't grow
│   │  adds ✅ only on confirmation
│   ▼
│  Claude Code in tmux pane receives input, processes it
│        │
│  Stop event fires → notify_discord.py
│        │  ipc({"type": "notify", "text": "**Claude:**\n...", "session_id": ..., "tmux_target": ...})
│        ▼
│  discord_bot.py handle_ipc_client
│   │  1. Posts Stop message to session thread (existing)
│   │  2. If session is synced: reads new JSONL messages since last_synced_line
│   │  3. Posts them as replies in the forum thread
│   │  4. On session end: posts "Session ended. Sync disabled." to forum thread
│   ▼
│  Bot adds ✅ on user's prompt message (via confirmer)
└─
```

### Session Thread Rename Flow

When the user runs `/rename new-name` in a running Claude Code session:

```
Claude Code writes updated name to ~/.claude/sessions/<pid>.json
Next hook event fires → notify_discord.py resolves new label
→ IPC message carries new session_label + same session_id
→ get_or_create_session_thread(session_id, new_label)
  → finds thread by session_id (stable key)
  → detects thread.name != "Session new-label"
  → calls thread.edit(name="Session new-label")
```

### `/sync` Command

| Invocation | Behavior |
|---|---|
| `/sync` | Select menu of active (PID-alive) sessions → picks one → starts sync |
| `/sync off` | Select menu of currently-synced sessions → picks one → stops sync |
| `/sync <name>` | Direct ON by session name, sessionId prefix, or hostname-prefixed label |
| `/sync <name> off` | Direct OFF |

When sync is ON:
1. Creates forum post named `"Sync: {hostname}-{session_label}"` in `DISCORD_SYNC_CHANNEL_ID`
2. Dumps full conversation history into the initial post
3. Stores sync state (forum thread ID, tmux target, last synced line) to `/tmp/claude_discord_sync.json`
4. On each Stop event: posts new messages as replies in the forum thread
5. On session end: posts "Session ended. Sync disabled." and auto-disables sync

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | — | Discord bot auth token |
| `DISCORD_CHANNEL_ID` | Yes | — | Channel for approval messages and threads |
| `DISCORD_INSPECT_CHANNEL_ID` | No | `DISCORD_CHANNEL_ID` | Channel for `/sessions`, `/history`, and `/summary` commands |
| `DISCORD_SUMMARY_CHANNEL_ID` | No | `DISCORD_INSPECT_CHANNEL_ID` | Forum channel where `/summary` posts reports |
| `DISCORD_SYNC_CHANNEL_ID` | No | `DISCORD_SUMMARY_CHANNEL_ID` | Forum channel where `/sync` creates session sync threads |
| `DISCORD_NOTIFY_USER_IDS` | No | — | Comma-separated user IDs auto-added to threads |
| `DISCORD_APPROVAL_TIMEOUT` | No | 120s | Timeout for normal approval decisions |
| `DISCORD_PLAN_APPROVAL_TIMEOUT` | No | 1800s | Timeout for ExitPlanMode plan feedback |
| `DISCORD_BOT_HOST` | No | Unix socket | TCP `host:port` for multi-machine IPC (e.g. `0.0.0.0:9876`). When set, both bot and shim use TCP instead of Unix socket. |
| `DISCORD_BOT_REMOTE` | No | — | When set (e.g. `true`), skip local bot spawn — the remote machine manages bot lifecycle. |

### Runtime files

| Path | Purpose |
|---|---|
| `/tmp/claude_discord.sock` | Unix socket for shim↔bot IPC |
| `/tmp/claude_discord.lock` | `fcntl.flock` lock file preventing duplicate bot instances |
| `/tmp/claude_discord_bot.pid` | Bot process PID |
| `/tmp/claude_discord_bot.ready` | Signal that bot is connected and listening |
| `/tmp/claude_discord_threads.json` | Persisted session→thread mapping (session_id keys) |
| `/tmp/claude_discord_tmux.json` | Cache of session_label→tmux_target for pane discovery |
| `/tmp/claude_discord_sync.json` | Persisted sync state (forum thread IDs, tmux targets) |
| `/tmp/claude_stop_<session>.txt` | External stop flag |
| `/tmp/claude_watchdog.pid` | Idle watchdog PID |
| `~/.claude/discord-decisions/` | Decision files (written by bot, read/polled by shim) |
| `~/.claude/sessions/*.json` | Session metadata (read by `/sessions`) |
| `~/.claude/projects/**/*.jsonl` | Conversation logs (read by `/history`) |
| `~/.claude/plans/*.md` | Plan files (read for ExitPlanMode display) |
| `~/.claude/history.jsonl` | Global command history (read by `/summary` to find sessions on a date) |

## Testing

```sh
# Run the test suite (from the repo root or hooks/ directory)
cd hooks && uv run pytest ../tests/ -v

# Simulate a hook event (dry run — no Discord connection needed)
python hooks/tests/simulate.py --dry-run hooks/tests/fixtures/permission_request_bash.json
```

- `tests/test_discord_bot.py` — 28 tests covering thread cache, IPC, interaction handling, usage summary, sync state, tmux send-keys (single and multiline), session resolution, thread cache migration.
- `tests/test_notify_discord.py` — 26 tests covering output formatting, bot lifecycle, IPC, IPC enrichment, text splitting, and fence handling.
- `hooks/tests/simulate.py` — manual CLI harness that pipes fixtures through the real hook logic.

## Dependencies

- **Production**: `discord.py>=2.0` — Discord API client library.
- **Dev**: `pytest>=8.0`, `pytest-asyncio>=0.23`.
- **Package manager**: `uv` (Astral). Lockfile: `hooks/uv.lock`.
- **Python**: `>=3.11`.

## How It's Distributed

Published as a Claude Code marketplace plugin from `TonyWu20/my-claude-marketplace`. Users install it via:

```
/plugins install TonyWu20/my-claude-marketplace#claude-code-discord-bot-hooks
```

The plugin manifest is at `.claude-plugin/plugin.json`.
