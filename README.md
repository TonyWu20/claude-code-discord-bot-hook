# Discord Hook for Claude Code

Routes Claude Code hook events to a Discord channel — approval requests, notifications,
and session stop signals — with interactive Approve/Deny buttons. Also provides slash
commands to inspect active sessions and conversation history.

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
| `PermissionRequest` | Posts Approve / Deny buttons; blocks until clicked or timeout |
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

### 1. Clone the repo

```sh
git clone https://github.com/TonyWu20/claude-code-discord-bot-hook ~/.claude/hooks
```

If you already have files in `~/.claude/hooks/`, clone to a subdirectory instead:

```sh
git clone https://github.com/TonyWu20/claude-code-discord-bot-hook ~/.claude/hooks/discord-bot
```

Then replace `~/.claude/hooks` with `~/.claude/hooks/discord-bot` in all paths below.

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
```

### 4. Install Dependencies

```sh
cd ~/.claude/hooks/hooks
uv sync
```

If `uv` is not installed:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 5. Register Hooks in `settings.json`

Open `~/.claude/settings.json` (create it if it does not exist) and add:

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/.venv/bin/python ~/.claude/hooks/notify_discord.py"
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/.venv/bin/python ~/.claude/hooks/notify_discord.py",
            "async": true
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/.venv/bin/python ~/.claude/hooks/notify_discord.py",
            "async": true
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/.venv/bin/python ~/.claude/hooks/notify_discord.py",
            "async": true
          }
        ]
      }
    ]
  }
}
```

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
- Manually test the bot: `.venv/bin/python discord_bot.py` (it will log errors to
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
