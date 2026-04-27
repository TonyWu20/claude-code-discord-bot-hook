# Architecture

> **Version guard**: This document reflects commit `27302fc` (2026-04-28, branch `main`).
> If the code has moved on, this may be stale. Run `git log ARCHITECTURE.md` to check
> whether it's been updated for the current HEAD, and update it if you make structural
> changes.

This document describes the architecture of `claude-code-discord-bot-hook`, a Claude Code plugin that bridges Claude Code CLI sessions to Discord. It serves as a map for anyone working on or extending the codebase.

## Project Overview

This is a **read-only monitor and remote permission handler** for Claude Code. When Claude Code fires hook events during a session, the plugin sends rich interactive messages to a Discord channel. A user can approve or deny Claude's actions (e.g., running a bash command, writing a file) from Discord — without being at the computer.

Notable characteristics:

- **Not a Discord chatbot** — it doesn't converse. It surfaces Claude Code's state and decisions.
- **Read-only monitor** by default; writes only happen through explicit button interactions (approve/deny).
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

- **Bot lifecycle**: `ensure_bot_running()` spawns `discord_bot.py` as a detached child if not already running.
- **IPC to bot**: Sends JSON over a Unix domain socket (`/tmp/claude_discord.sock`), reads back one JSON response line.
- **Event dispatch**: Routes on `hook_event_name`:
  - `Stop` / `SubagentStop` — fire-and-forget notification of the assistant's last message.
  - `PermissionRequest` / `PreToolUse` — blocking approval flow: sends a rich message with buttons to Discord, polls for a decision file, prints the decision JSON to stdout.
  - Other events (e.g., `Notification`) are silently ignored.
- **Special tool handling**:
  - `AskUserQuestion` renders multi-choice questions that become Discord Select menus.
  - `ExitPlanMode` reads the current plan file from `~/.claude/plans/` and sends it as native markdown.
  - `Bash` shows the command in a code block, with chunking for long commands.
- **Idle watchdog**: When `--idle-from-stdin` is passed (via `UserPromptSubmit` hook), spawns a detached child that posts "Claude is waiting for input" after 5 minutes of inactivity.
- **Stop flag**: Checks `/tmp/claude_stop_<session>.txt` before processing approvals — allows external cancellation.

### `discord_bot.py` — Persistent Discord Bot

This is a long-lived `discord.py` bot that holds the Discord WebSocket connection. Started once by `notify_discord.py` when first needed.

Key responsibilities:

- **IPC socket server**: Listens on `/tmp/claude_discord.sock`. Each connection processes one JSON request:
  - `{"type": "notify", ...}` — posts a text message to the session's Discord thread.
  - `{"type": "approve", ...}` — posts a message with interactive buttons, then polls a decision file and returns the result.
- **Slash commands** (via `app_commands`):
  - `/sessions` — lists recent Claude Code sessions.
  - `/history [session] [tail]` — shows recent conversation messages from a session's JSONL file, formatted in a thread.
  - `/summary [date]` — aggregates per-project token usage, model breakdown, and session time for a given UTC date; posts the report as a new forum post.
- **Button/modal interactions**: Handles `approve`, `deny`, `suggest` (permission edits), `askq` (question answering), `plan_feedback` (ExitPlanMode rejection), and `edit_rule` (rule editing before approval).
- **Session threads**: Creates one Discord thread per Claude Code session, persisted to `/tmp/claude_discord_threads.json`.

## Data Flow

### Notification Flow (fire-and-forget)

```
Claude Code fires hook event (e.g., Stop)
→ notify_discord.py reads JSON from stdin
→ ipc({"type": "notify", ...}) → Unix socket → discord_bot.py
→ bot.posts message to session's Discord thread
→ notify_discord.py exits
```

### Approval Flow (blocking)

```
Claude Code fires PermissionRequest or PreToolUse
→ notify_discord.py builds rich message with tool details
→ ipc({"type": "approve", ...}) → Unix socket → discord_bot.py
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

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | — | Discord bot auth token |
| `DISCORD_CHANNEL_ID` | Yes | — | Channel for approval messages and threads |
| `DISCORD_INSPECT_CHANNEL_ID` | No | `DISCORD_CHANNEL_ID` | Channel for `/sessions`, `/history`, and `/summary` commands |
| `DISCORD_SUMMARY_CHANNEL_ID` | No | `DISCORD_INSPECT_CHANNEL_ID` | Forum channel where `/summary` posts reports |
| `DISCORD_NOTIFY_USER_IDS` | No | — | Comma-separated user IDs auto-added to threads |
| `DISCORD_APPROVAL_TIMEOUT` | No | 120s | Timeout for normal approval decisions |
| `DISCORD_PLAN_APPROVAL_TIMEOUT` | No | 1800s | Timeout for ExitPlanMode plan feedback |

### Runtime files

| Path | Purpose |
|---|---|
| `/tmp/claude_discord.sock` | Unix socket for shim↔bot IPC |
| `/tmp/claude_discord_bot.pid` | Bot process PID |
| `/tmp/claude_discord_bot.ready` | Signal that bot is connected and listening |
| `/tmp/claude_discord_threads.json` | Persisted session→thread mapping |
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

- `tests/test_discord_bot.py` — 18 tests covering thread cache, IPC, interaction handling, and usage summary.
- `tests/test_notify_discord.py` — 9 tests covering output formatting, bot lifecycle, and IPC.
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
