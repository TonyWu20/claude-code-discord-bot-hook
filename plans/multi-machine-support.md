# Multi-Machine Support (TCP IPC)

## Goal

Allow multiple machines running Claude Code to share a single Discord bot instance over any network (ZeroTier, Tailscale, LAN, VPN, public internet with firewall rules). Default behavior (Unix socket, local-only) stays unchanged — this is fully backward-compatible.

## Problem

The project currently uses Unix sockets and local filesystem for IPC, making it single-machine:

- `notify_discord.py` → Unix socket → `discord_bot.py`
- Bot lifecycle managed via PID file + `os.kill()` — local only
- All state (thread cache, decision files) written to local `/tmp/` and `~/.claude/`

## Solution

Add TCP transport option. When `DISCORD_BOT_HOST` is set, both the bot and shim use TCP instead of Unix socket. When unset (default), behavior is identical to today.

**Transport detection logic:**
- If `DISCORD_BOT_HOST` is empty/unset → use Unix socket (current behavior, fully backward-compatible)
- If `DISCORD_BOT_HOST` is set → use TCP to `host:port`

## Files to Change

### 1. `discord_bot.py` — `run_socket_server()`

- If `DISCORD_BOT_HOST` is set: use `asyncio.start_server` bound to host:port
- If unset: keep existing `asyncio.start_unix_server(path=SOCKET_PATH)`

**New env var:** `DISCORD_BOT_HOST` — e.g., `0.0.0.0:9876` or `192.168.1.50:9876`

The PID file + ready file stay the same in both modes.

### 2. `notify_discord.py` — `ipc()` and `ensure_bot_running()`

- `ipc()`: if `DISCORD_BOT_HOST` is set, connect via `socket.socket(AF_INET)` instead of `AF_UNIX`. Same JSON-line protocol, just over TCP.
- `ensure_bot_running()`: if `DISCORD_BOT_HOST` is set, skip the local bot spawn (the remote machine manages its own bot lifecycle).

**New env var:** `DISCORD_BOT_REMOTE=true` (optional) — when set, skip local bot spawn.

### 3. `hooks/hooks.json`

No change needed — env vars are already forwarded from Claude Code's hook invocation.

## Why This Is Network-Agnostic

The `DISCORD_BOT_HOST` value is just a host:port. It works over:

| Network | Example |
|---|---|
| ZeroTier | `10.147.17.1:9876` |
| Tailscale | `100.x.x.x:9876` |
| LAN | `192.168.1.50:9876` |
| VPN | `10.0.0.1:9876` |
| Public (with firewall) | `your.domain.com:9876` |

No ZeroTier-specific code, no hardcoded IPs, no assumptions about the network topology.

## How the Approval Flow Works Over TCP

```
Shim (machine A)                   Bot (machine B)
  │                                   │
  ├─ IPC "approve" ──TCP──> ──────────┤
  │                                   ├─ posts message with buttons
  │                                   ├─ polls DECISION_DIR for file
  │                                   │  ← User clicks button
  │                                   │  ← writes decision file locally
  │                                   ├─ reads file, returns via TCP
  ├─ <──TCP── result ────────────────┤
  └─ prints result, exits            └─
```

The decision file polling happens **in the bot** (`handle_ipc_client` lines 700-714), not the shim. The shim just blocks on `ipc()` reading a response. So the entire approval flow works across machines with zero changes to the decision logic — it's already decoupled.

## Setup

**Server machine** (runs the bot):
```
DISCORD_BOT_TOKEN=x \
DISCORD_BOT_HOST=0.0.0.0:9876 \
discord_bot.py
```

**Client machines** (just the hook shim, no bot):
```
DISCORD_BOT_TOKEN=x \
DISCORD_BOT_HOST=192.168.1.50:9876 \
DISCORD_BOT_REMOTE=true
```

Clients don't run `discord_bot.py` at all — the shim connects directly to the server. If the server is unreachable, `ipc()` returns `None` and the hook falls through to Claude's local decision (same as a timeout today).

## Edge Cases

- **Server down** → shim can't connect → `ipc()` returns `None` → hook falls through to local Claude prompt (no Discord approval). Same UX as a bot crash today.
- **Idle watchdog** → each machine runs its own watchdog locally. It sends a `notify` over TCP — no change needed.
- **Thread naming** → thread names use the session label. Sessions from different machines already have unique IDs, so they won't collide.
- **Slash commands** → `/sessions`, `/history`, `/summary` run on the server machine and only see that machine's sessions. Acceptable since slash commands are secondary to the approval flow.
- **Stop flag** (`/tmp/claude_stop_<session>.txt`) → checked in the shim before IPC, so each machine handles its own. No change needed.

## Implementation Steps

1. Add `DISCORD_BOT_HOST` constant to both files
2. In `discord_bot.py`: branch on `DISCORD_BOT_HOST` → TCP vs Unix socket in `run_socket_server()`
3. In `notify_discord.py`: branch in `ipc()` to use AF_INET or AF_UNIX based on `DISCORD_BOT_HOST`
4. In `notify_discord.py`: branch in `ensure_bot_running()` to skip spawn when `DISCORD_BOT_REMOTE=true`
5. Update `ARCHITECTURE.md` and `CHANGE_LOG.md`
