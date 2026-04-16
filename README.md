# Discord Hook for Claude Code

Routes Claude Code hook events to a Discord channel — approval requests, notifications,
and session stop signals — with interactive Approve/Deny buttons.

## Architecture

Two scripts work together:

```
Claude Code hook event
        │
        ▼
notify_discord.py          ← thin hook shim (spawned per-event, exits fast)
        │  Unix socket IPC
        ▼
discord_bot.py             ← persistent bot process (long-lived, holds connection)
        │
        ▼
Discord channel / thread
```

**`notify_discord.py`** — the hook entry point. Claude Code invokes it for every
registered event. It ensures the bot process is running, serialises the event into
a JSON message, and sends it over a Unix domain socket. For `PermissionRequest` events
it blocks until the bot replies with a decision; all other events are fire-and-forget.

**`discord_bot.py`** — a long-lived `discord.py` bot that owns the WebSocket
connection to Discord. It listens on the Unix socket for IPC messages from the shim,
posts messages/buttons to a per-session thread, and forwards button interactions back
as JSON responses to the waiting shim.

## Supported Hook Events

| Event               | Behaviour                                                     |
| ------------------- | ------------------------------------------------------------- |
| `PermissionRequest` | Posts Approve / Deny buttons; blocks until clicked or timeout |
| `Notification`      | Posts a notification message to the session thread            |
| `Stop`              | Posts the assistant's final message to the session thread     |
| `SubagentStop`      | Same as `Stop`, labelled with the agent type                  |

## Session Threads

Each Claude Code session gets its own Discord thread named `claude: <session>`.
The session label is resolved from `~/.claude/sessions/` using the parent PID or
session ID. Thread IDs are persisted in `/tmp/claude_discord_threads.json` so the
bot reuses the same thread across reconnects within a session.

## Stop Flag

Sending `!stop [reason]` in a session thread writes a flag file at
`/tmp/claude_stop_<session>.txt`. On the next hook invocation the shim reads this
file, deletes it, and returns `deny` — causing Claude to abort the pending tool use.

## Setup

### 1. Clone the repo

Clone this repository anywhere convenient. The examples below use `~/.claude/hooks`
as the install location, but any path works — just substitute your chosen path in
steps 4 and 5.

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

The hook reads credentials from the environment. Add these to your shell profile
(e.g. `~/.config/fish/config.fish`, `~/.zshenv`):

```sh
export DISCORD_BOT_TOKEN="your-bot-token"
export DISCORD_CHANNEL_ID="channel-snowflake-id"
```

Optional tuning:

```sh
export DISCORD_APPROVAL_TIMEOUT=120   # seconds to wait for button press (default: 120)
export DISCORD_DELETE_AFTER=300       # seconds before auto-deleting messages (default: 300)
```

### 4. Install Dependencies

Dependencies are declared in `hooks/pyproject.toml`. Use `uv` to create the
virtualenv and install them:

```sh
cd ~/.claude/hooks/hooks
uv sync
```

`uv` creates `.venv/` automatically and pins the resolved versions in
`uv.lock`. To add or upgrade packages, edit `pyproject.toml` and re-run
`uv sync`.

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

| Path                               | Purpose                               |
| ---------------------------------- | ------------------------------------- |
| `/tmp/claude_discord.sock`         | Unix socket between shim and bot      |
| `/tmp/claude_discord_bot.pid`      | PID of the running bot process        |
| `/tmp/claude_discord_threads.json` | Persisted session → thread ID mapping |
| `/tmp/claude_stop_<session>.txt`   | Stop-flag written by `!stop` command  |

## Troubleshooting

**No messages appear in Discord**

- Confirm `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` are exported in the same
  shell environment that runs Claude Code.
- Check whether the bot process is running: `cat /tmp/claude_discord_bot.pid` then
  `ps -p <pid>`.
- Manually test the bot: `.venv/bin/python discord_bot.py` (it will log errors to
  stderr).

**Approval request times out**

- Increase `DISCORD_APPROVAL_TIMEOUT`. The shim waits `timeout + 5` seconds for the
  socket reply; after that Claude decides locally.

**Bot accumulates stale threads**

- Delete `/tmp/claude_discord_threads.json` to force the bot to create fresh threads
  on next start. Old threads in Discord can be archived manually.

**Permission denied on socket**

- Delete `/tmp/claude_discord.sock` and restart the bot. The socket is recreated on
  each bot start.
