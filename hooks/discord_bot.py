#!/usr/bin/env python3
"""
Discord bot for Claude Code session inspection.

Commands:
  /sessions          — list active/recent sessions (ephemeral, inspect channel)
  /history [session] — show conversation history in a thread (inspect channel)

Required env vars:
  DISCORD_BOT_TOKEN
  DISCORD_CHANNEL_ID        — approvals channel
  DISCORD_INSPECT_CHANNEL_ID — inspection channel (falls back to DISCORD_CHANNEL_ID)

The bot handles Approve/Deny button interactions from notify_discord.py
by writing decision files to ~/.claude/discord-decisions/.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
INSPECT_CHANNEL_ID = int(os.environ.get("DISCORD_INSPECT_CHANNEL_ID", CHANNEL_ID))
APPROVAL_TIMEOUT = int(os.environ.get("DISCORD_APPROVAL_TIMEOUT", "120"))
SOCKET_PATH = "/tmp/claude_discord.sock"
PID_FILE = "/tmp/claude_discord_bot.pid"
READY_FILE = "/tmp/claude_discord_bot.ready"

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
DECISION_DIR = Path.home() / ".claude" / "discord-decisions"
DECISION_DIR.mkdir(exist_ok=True)
THREAD_CACHE_FILE = Path("/tmp/claude_discord_threads.json")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# session_label -> discord.Thread (in-memory cache)
_session_threads: dict[str, discord.Thread] = {}


def _load_thread_ids() -> dict[str, int]:
    try:
        return json.loads(THREAD_CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_thread_id(session: str, thread_id: int) -> None:
    ids = _load_thread_ids()
    ids[session] = thread_id
    THREAD_CACHE_FILE.write_text(json.dumps(ids))


# ── session helpers ────────────────────────────────────────────────────────────

def load_sessions() -> list[dict]:
    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            d["_pid"] = f.stem
            sessions.append(d)
        except (json.JSONDecodeError, OSError):
            continue
    sessions.sort(key=lambda s: s.get("startedAt", 0), reverse=True)
    return sessions


def find_conversation_file(session_id: str) -> Path | None:
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob("*.jsonl"):
            if f.stem.startswith(session_id):
                return f
            try:
                first = json.loads(f.read_text().splitlines()[0])
                if first.get("sessionId") == session_id:
                    return f
            except (json.JSONDecodeError, OSError, IndexError):
                continue
    return None


def extract_messages(jsonl_path: Path) -> list[dict]:
    messages = []
    for line in jsonl_path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        role = d["type"]
        msg = d.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    inp = json.dumps(block.get("input", {}), indent=2, ensure_ascii=False)
                    if len(inp) > 500:
                        inp = inp[:500] + "\n…"
                    parts.append(f"🔧 **{name}**\n```json\n{inp}\n```")
                elif block.get("type") == "tool_result":
                    result = block.get("content", "")
                    if isinstance(result, list):
                        result = "\n".join(b.get("text", "") for b in result if isinstance(b, dict))
                    result = str(result).strip()
                    if len(result) > 500:
                        result = result[:500] + "\n…"
                    parts.append(f"📤 **Result**\n```\n{result}\n```")
            content = "\n".join(parts)
        if not content.strip():
            continue
        messages.append({
            "role": role,
            "content": str(content),
            "timestamp": d.get("timestamp", ""),
        })
    return messages


def format_message(m: dict) -> str:
    if m["role"] == "user":
        header = "## 👤 You"
    else:
        header = "## 🤖 Claude"
    content = m["content"]
    if len(content) > 1800:
        content = content[:1800] + "\n…"
    return f"{header}\n{content}"


# ── slash commands ─────────────────────────────────────────────────────────────

@tree.command(name="sessions", description="List active/recent Claude Code sessions")
async def slash_sessions(interaction: discord.Interaction):
    if interaction.channel_id != INSPECT_CHANNEL_ID:
        await interaction.response.send_message("Use the inspect channel.", ephemeral=True)
        return
    sessions = load_sessions()
    if not sessions:
        await interaction.response.send_message("No sessions found.", ephemeral=True)
        return
    lines = []
    for i, s in enumerate(sessions[:10]):
        sid = s.get("sessionId", "?")[:8]
        cwd = s.get("cwd", "?")
        ts = s.get("startedAt", 0)
        started = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        lines.append(f"`{i}` `{sid}` {started} — `{cwd}`")
    await interaction.response.send_message("**Active/recent sessions:**\n" + "\n".join(lines), ephemeral=True)


@tree.command(name="history", description="Show conversation history for a session")
@app_commands.describe(session="Session index (default 0) or sessionId prefix")
async def slash_history(interaction: discord.Interaction, session: str = "0"):
    if interaction.channel_id != INSPECT_CHANNEL_ID:
        await interaction.response.send_message("Use the inspect channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    sessions = load_sessions()
    if not sessions:
        await interaction.followup.send("No sessions found.")
        return

    sess = None
    if session.isdigit():
        idx = int(session)
        if idx < len(sessions):
            sess = sessions[idx]
    else:
        for s in sessions:
            if s.get("sessionId", "").startswith(session):
                sess = s
                break

    if not sess:
        await interaction.followup.send(f"Session `{session}` not found.")
        return

    session_id = sess.get("sessionId", "")
    conv_file = find_conversation_file(session_id)
    if not conv_file:
        await interaction.followup.send(f"No conversation file found for session `{session_id[:8]}`.")
        return

    messages = extract_messages(conv_file)
    if not messages:
        await interaction.followup.send("No messages in this session yet.")
        return

    channel = bot.get_channel(INSPECT_CHANNEL_ID)
    thread = await channel.create_thread(
        name=f"Session {session_id[:8]} — {len(messages)} messages",
        type=discord.ChannelType.public_thread,
    )
    for m in messages:
        await thread.send(format_message(m))

    await interaction.followup.send(f"History opened in {thread.mention}")


# ── session thread helpers ─────────────────────────────────────────────────────

async def get_or_create_session_thread(session: str) -> discord.Thread:
    # Check in-memory cache first
    if session in _session_threads:
        thread = _session_threads[session]
        try:
            await thread.fetch()
            return thread
        except discord.NotFound:
            del _session_threads[session]

    # Check persisted thread IDs
    thread_ids = _load_thread_ids()
    if session in thread_ids:
        channel = bot.get_channel(CHANNEL_ID)
        try:
            thread = await bot.fetch_channel(thread_ids[session])
            _session_threads[session] = thread
            return thread
        except (discord.NotFound, discord.HTTPException):
            pass

    # Create new thread
    channel = bot.get_channel(CHANNEL_ID)
    thread = await channel.create_thread(
        name=f"Session {session}",
        type=discord.ChannelType.public_thread,
    )
    _session_threads[session] = thread
    _save_thread_id(session, thread.id)
    return thread


# ── IPC socket server ──────────────────────────────────────────────────────────

async def handle_ipc_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        req = json.loads(line)
    except Exception:
        writer.close()
        return

    msg_type = req.get("type")
    session = req.get("session", "unknown")
    text = req.get("text", "")

    if bot.get_channel(CHANNEL_ID) is None:
        writer.write(b'{"ok": false, "error": "bot not connected"}\n')
        await writer.drain()
        writer.close()
        return

    if msg_type == "notify":
        thread = await get_or_create_session_thread(session)
        await thread.send(text)
        writer.write(b'{"ok": true}\n')

    elif msg_type == "approve":
        request_id = req.get("request_id", "")
        thread = await get_or_create_session_thread(session)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Approve", style=discord.ButtonStyle.success,
            custom_id=f"approve:{request_id}",
        ))
        view.add_item(discord.ui.Button(
            label="Deny", style=discord.ButtonStyle.danger,
            custom_id=f"deny:{request_id}",
        ))
        await thread.send(text, view=view)

        # Poll for decision file
        decision_file = DECISION_DIR / f"{request_id}.json"
        deadline = time.monotonic() + APPROVAL_TIMEOUT
        result = None
        while time.monotonic() < deadline:
            if decision_file.exists():
                try:
                    result = json.loads(decision_file.read_text())
                    decision_file.unlink(missing_ok=True)
                except (json.JSONDecodeError, OSError):
                    pass
                break
            await asyncio.sleep(0.5)

        if result is None:
            result = {"decision": "ask", "reason": "Timed out waiting for Discord response"}
        writer.write((json.dumps(result) + "\n").encode())

    else:
        writer.write(b'{"ok": false}\n')

    await writer.drain()
    writer.close()


async def run_socket_server() -> None:
    Path(SOCKET_PATH).unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle_ipc_client, path=SOCKET_PATH)
    Path(PID_FILE).write_text(str(os.getpid()))
    Path(READY_FILE).write_text("ready")
    async with server:
        await server.serve_forever()


# ── button interactions (Approve/Deny from notify_discord.py) ──────────────────

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get("custom_id", "")
    if ":" not in custom_id:
        return
    action, request_id = custom_id.split(":", 1)
    if action not in ("approve", "deny"):
        return

    if action == "approve":
        decision = {"decision": "allow", "reason": "Approved via Discord"}
        await interaction.response.send_message(f"✅ Approved `{request_id}`", ephemeral=True)
    else:
        decision = {"decision": "deny", "reason": "Denied via Discord"}
        await interaction.response.send_message(f"❌ Denied `{request_id}`", ephemeral=True)

    (DECISION_DIR / f"{request_id}.json").write_text(json.dumps(decision))


@bot.event
async def on_message(message):
    print(f"[msg] #{message.channel.id} {message.author}: {message.content!r}")
    await bot.process_commands(message)


@bot.event
async def on_ready():
    print(f"Discord bot ready as {bot.user}")
    asyncio.create_task(run_socket_server())


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
