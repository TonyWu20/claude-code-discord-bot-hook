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

import json
import os
from pathlib import Path
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
INSPECT_CHANNEL_ID = int(os.environ.get("DISCORD_INSPECT_CHANNEL_ID", CHANNEL_ID))

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
DECISION_DIR = Path.home() / ".claude" / "discord-decisions"
DECISION_DIR.mkdir(exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


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
    await tree.sync()
    print(f"Discord bot ready as {bot.user}, slash commands synced")


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
