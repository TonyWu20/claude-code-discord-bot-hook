# Discord Hook for Claude Code

Routes Claude Code hook events to a Discord channel — approval requests, notifications,
and session stop signals — with interactive Approve/Deny buttons. Also provides slash
commands to inspect active sessions and conversation history.

## Highlights

- **No Claude subscription required** — runs entirely on Claude Code's hook system; all you need is a Discord bot token.
- **Not a full Discord-based Claude replacement** — this is a read-only monitor and permission handler. Sending user prompts back to a session would require injecting into the CLI, which is intentionally out of scope.
- **Remote session monitoring** — approve or deny permission requests from your phone while away from your computer. Requests time out after 120 seconds with graceful fallback.
- **`AskUserQuestion` support** — Claude's multi-choice questions appear as Discord Select menus; an "Answer with text" button opens a Modal for free-text responses.
- **`ExitPlanMode` support** — plan approval requests show the full plan content in Discord with Approve, Reject, and Give Feedback buttons. "Give Feedback" opens a Modal so you can type revision instructions directly.
- **History view with tail** — the `/history` slash command supports an optional `tail` parameter to see only the latest updates from your session, rendered in a Discord thread.
- **Thread-based organization** — each session gets its own Discord thread, labeled with the session name set via `/rename` in Claude Code, keeping your server tidy.

## Architecture

```
Claude Code hook event
        │
        ▼
notify_discord.py          ← thin hook shim (spawned per-event, exits fast)
        │  writes decision file
        ▼
~/.claude/discord-decisions/   ← shared decision directory
        ▲
        │  reads/writes
discord_bot.py             ← persistent bot process (long-lived, holds connection)
        │
        ▼
Discord channel / thread
```

**`notify_discord.py`** — the hook entry point. Claude Code invokes it for every
registered event. For `PermissionRequest` events it polls
`~/.claude/discord-decisions/<request_id>.json` until the bot writes a decision; all
other events are fire-and-forget.

**`discord_bot.py`** — a long-lived `discord.py` bot. It listens for button
interactions from Discord, writes decision files to `~/.claude/discord-decisions/`,
and exposes slash commands for session inspection.

## Supported Hook Events

| Event               | Behaviour                                                     |
| ------------------- | ------------------------------------------------------------- |
| `PermissionRequest` | Posts Approve / Deny / permission-suggestion buttons; blocks until clicked or timeout |
| `PreToolUse`        | Intercepts `AskUserQuestion` (Select menus + text modal) and `ExitPlanMode` (plan content + Approve / Reject / Give Feedback) |
| `Notification`      | Posts a notification message to the session thread            |
| `Stop`              | Posts the assistant's final message to the session thread     |
| `SubagentStop`      | Same as `Stop`, labelled with the agent type                  |

## Slash Commands

| Command             | Channel          | Description                                      |
| ------------------- | ---------------- | ------------------------------------------------ |
| `/sessions`         | Inspect channel  | List active/recent Claude Code sessions          |
| `/history [session]`| Inspect channel  | Show conversation history for a session in a thread |

`session` can be a numeric index (default `0`) or a `sessionId` prefix.

## Setup

### 1. Install uv (required)

The hook scripts use `uv run` to auto-manage the Python environment.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verify: `uv --version`

### 2. Install from Claude Code Marketplace

In Claude Code, run:

```
/plugins install TonyWu20/my-claude-marketplace#claude-code-discord-bot-hooks
```

This automatically installs the plugin and registers all hook events in your Claude Code configuration.

### 2. Create a Discord Bot

1. Go to <https://discord.com/developers/applications> and create a new application.
2. Under **Bot**, enable **Message Content Intent**.
3. Under **OAuth2 → URL Generator**, select scopes `bot` and permissions
   `Send Messages`, `Create Public Threads`, `Read Message History`.
4. Invite the bot to your server with the generated URL.
5. Copy the bot token.

### 3. Set Environment Variables

Add these to your shell profile (e.g. `~/.config/fish/config.fish`, `~/.zshenv`):

```sh
export DISCORD_BOT_TOKEN="your-bot-token"
export DISCORD_CHANNEL_ID="channel-snowflake-id"
```

Optional:

```sh
export DISCORD_INSPECT_CHANNEL_ID="inspect-channel-id"  # defaults to DISCORD_CHANNEL_ID
export DISCORD_NOTIFY_USER_IDS="123456789012345678"      # comma-separated Discord user IDs to auto-add to session threads
```

`DISCORD_INSPECT_CHANNEL_ID` is where the `/sessions` and `/history` slash commands run. If not set, they fall back to `DISCORD_CHANNEL_ID`.

`DISCORD_NOTIFY_USER_IDS` tells the bot which users to automatically add to each new session thread. This is essential if you want to receive notifications promptly — without being added to the thread, you won't get push notifications for messages posted there.

### 4. Verify Setup

The plugin hooks use `uv run` to automatically create the Python venv and install dependencies
on first invocation — no manual `uv sync` needed. Confirm `uv` is on your PATH.

## Runtime Files

| Path                                    | Purpose                                      |
| --------------------------------------- | -------------------------------------------- |
| `~/.claude/discord-decisions/<id>.json` | Decision files written by the bot            |
| `~/.claude/sessions/*.json`             | Session metadata read by `/sessions`         |
| `~/.claude/projects/**/*.jsonl`         | Conversation files read by `/history`        |

## Troubleshooting

**No messages appear in Discord**

- Confirm `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` are exported in the same
  shell environment that runs Claude Code.
- Manually test the bot: `cd ~/.claude/hooks/hooks && uv run python discord_bot.py` (it will log errors to
  stderr).

**Approval request times out**

- The shim polls for a decision file; ensure the bot process is running and the
  `~/.claude/discord-decisions/` directory is writable.

**`/sessions` or `/history` returns nothing**

- Confirm `~/.claude/sessions/` contains `.json` session files.
- Confirm `DISCORD_INSPECT_CHANNEL_ID` (or `DISCORD_CHANNEL_ID`) matches the channel
  where you run the slash command.

**Slash commands not appearing**

- Commands are synced on bot startup via `tree.sync()`. Restart the bot and wait a
  few minutes for Discord to propagate the commands.
