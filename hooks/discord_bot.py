#!/usr/bin/env python3
"""
Persistent Discord bot for Claude Code hooks.

Env vars:
  DISCORD_BOT_TOKEN   — bot token
  DISCORD_CHANNEL_ID  — channel ID for approval messages
  DISCORD_DELETE_AFTER — seconds before auto-deleting messages (default: 300)
  DISCORD_APPROVAL_TIMEOUT — seconds to wait for button press (default: 120)
"""

import asyncio
import json
import os
import signal
from pathlib import Path

import discord

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
DELETE_AFTER = int(os.environ.get("DISCORD_DELETE_AFTER", "300"))
APPROVAL_TIMEOUT = int(os.environ.get("DISCORD_APPROVAL_TIMEOUT", "120"))
SOCKET_PATH = "/tmp/claude_discord.sock"
PID_FILE = "/tmp/claude_discord_bot.pid"
THREADS_FILE = "/tmp/claude_discord_threads.json"

# request_id -> asyncio.Future[dict]
pending: dict[str, asyncio.Future] = {}
# request_id -> Future waiting for deny reason text
deny_pending: dict[str, asyncio.Future] = {}
# session_label -> thread_id
session_threads: dict[str, int] = {}


def load_session_threads() -> None:
    try:
        data = json.loads(Path(THREADS_FILE).read_text())
        session_threads.update({k: int(v) for k, v in data.items()})
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass


def save_session_threads() -> None:
    Path(THREADS_FILE).write_text(json.dumps(session_threads))

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def delete_later(msg: discord.Message) -> None:
    await asyncio.sleep(DELETE_AFTER)
    try:
        await msg.delete()
    except discord.NotFound:
        pass


async def get_or_create_thread(session: str) -> discord.Thread:
    channel = client.get_channel(CHANNEL_ID)
    thread_id = session_threads.get(session)
    if thread_id:
        thread = client.get_channel(thread_id)
        if thread:
            return thread
    # Fallback: search active threads by name to avoid duplicates
    try:
        active = await channel.guild.active_threads()
        for t in active:
            if t.parent_id == CHANNEL_ID and t.name == f"claude: {session}":
                session_threads[session] = t.id
                save_session_threads()
                return t
    except Exception:
        pass
    # Create a new public thread in the channel
    thread = await channel.create_thread(name=f"claude: {session}", type=discord.ChannelType.public_thread)
    session_threads[session] = thread.id
    save_session_threads()
    return thread


@client.event
async def on_ready() -> None:
    load_session_threads()
    Path(PID_FILE).write_text(str(os.getpid()))
    asyncio.get_event_loop().create_task(ipc_server())


@client.event
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.component:
        return
    data = interaction.data or {}
    custom_id: str = data.get("custom_id", "")
    await interaction.response.defer()

    if custom_id.startswith("approve:"):
        request_id = custom_id[len("approve:"):]
        fut = pending.pop(request_id, None)
        if fut and not fut.done():
            # Remove buttons from original message and confirm
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            thread = interaction.channel
            if thread:
                confirm = await thread.send(f"✅ **Approved** `[{request_id}]`")
                asyncio.get_event_loop().create_task(delete_later(confirm))
            fut.set_result({"decision": "allow", "reason": "Approved via Discord"})

    elif custom_id.startswith("deny:"):
        request_id = custom_id[len("deny:"):]
        fut = pending.pop(request_id, None)
        if fut and not fut.done():
            # Remove buttons from original message
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            reason_fut: asyncio.Future = asyncio.get_event_loop().create_future()
            deny_pending[request_id] = reason_fut
            thread = interaction.channel
            if thread:
                msg = await thread.send(
                    f"❌ **Denied** `[{request_id}]`\n\nReply with a reason for Claude (30 s, or just wait):"
                )
                asyncio.get_event_loop().create_task(delete_later(msg))
            try:
                reason = await asyncio.wait_for(reason_fut, timeout=30)
            except asyncio.TimeoutError:
                reason = "Denied via Discord"
            deny_pending.pop(request_id, None)
            fut.set_result({"decision": "deny", "reason": reason})


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author == client.user:
        return
    # Only handle messages in known session threads
    session = next((s for s, tid in session_threads.items() if tid == message.channel.id), None)
    if session is None:
        return
    # Handle deny reason replies
    for request_id, fut in list(deny_pending.items()):
        if not fut.done():
            fut.set_result(message.content)
            return
    # Handle !stop command
    if message.content.startswith("!stop"):
        reason = message.content[len("!stop"):].strip() or "Stopped via Discord"
        flag_path = Path(f"/tmp/claude_stop_{session}.txt")
        flag_path.write_text(reason)
        confirm = await message.channel.send(f"🛑 Stop flag set for session `{session}`: {reason}")
        asyncio.get_event_loop().create_task(delete_later(confirm))


async def handle_ipc(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await reader.readline()
        req = json.loads(raw)
        session = req.get("session", "unknown")
        channel = client.get_channel(CHANNEL_ID)
        if channel is None:
            writer.write(b'{"decision":"ask","reason":"Bot channel not found"}\n')
            await writer.drain()
            writer.close()
            return

        thread = await get_or_create_thread(session)

        if req["type"] == "notify":
            msg = await thread.send(req["text"])
            asyncio.get_event_loop().create_task(delete_later(msg))
            writer.write(b"\n")
            await writer.drain()

        elif req["type"] == "approve":
            request_id = req["request_id"]
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(
                label="✅ Approve", style=discord.ButtonStyle.success,
                custom_id=f"approve:{request_id}"
            ))
            view.add_item(discord.ui.Button(
                label="❌ Deny", style=discord.ButtonStyle.danger,
                custom_id=f"deny:{request_id}"
            ))
            msg = await thread.send(req["text"], view=view)
            asyncio.get_event_loop().create_task(delete_later(msg))

            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            pending[request_id] = fut
            try:
                result = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
            except asyncio.TimeoutError:
                pending.pop(request_id, None)
                result = {"decision": "ask", "reason": "Discord approval timed out — decide locally"}

            writer.write((json.dumps(result) + "\n").encode())
            await writer.drain()

    except Exception as e:
        writer.write((json.dumps({"decision": "ask", "reason": str(e)}) + "\n").encode())
        await writer.drain()
    finally:
        writer.close()


async def ipc_server() -> None:
    if Path(SOCKET_PATH).exists():
        Path(SOCKET_PATH).unlink()
    server = await asyncio.start_unix_server(handle_ipc, path=SOCKET_PATH)
    async with server:
        await server.serve_forever()


def main() -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        raise SystemExit("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set")

    def _shutdown(sig, frame):
        Path(PID_FILE).unlink(missing_ok=True)
        Path(SOCKET_PATH).unlink(missing_ok=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    client.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
