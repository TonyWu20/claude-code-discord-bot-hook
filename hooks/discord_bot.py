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
  DISCORD_NOTIFY_USER_IDS   — comma-separated user IDs to auto-add to session threads (optional)

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
PLAN_APPROVAL_TIMEOUT = int(os.environ.get("DISCORD_PLAN_APPROVAL_TIMEOUT", "900"))
_NOTIFY_USER_IDS: list[int] = [
    int(uid.strip())
    for uid in os.environ.get("DISCORD_NOTIFY_USER_IDS", "").split(",")
    if uid.strip()
]
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
# request_id -> list of permission_suggestions entries
_pending_suggestions: dict[str, list] = {}
# request_id -> tool_input (for ExitPlanMode, echoed back as updatedInput)
_pending_tool_input: dict[str, dict] = {}
# request_id -> {questions: [...], answers: {question_text: label(s)}}
_pending_questions: dict[str, dict] = {}


def _load_thread_ids() -> dict[str, int]:
    try:
        return json.loads(THREAD_CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_thread_id(session: str, thread_id: int) -> None:
    ids = _load_thread_ids()
    ids[session] = thread_id
    THREAD_CACHE_FILE.write_text(json.dumps(ids))


async def _add_notify_users(thread: discord.Thread) -> None:
    for uid in _NOTIFY_USER_IDS:
        try:
            await thread.add_user(discord.Object(id=uid))
        except discord.HTTPException as e:
            print(f"[warn] Failed to add user {uid} to thread {thread.id}: {e}")


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
@app_commands.describe(session="Session index (default 0) or sessionId prefix", tail="Show only last 10 rounds (default False)")
async def slash_history(interaction: discord.Interaction, session: str = "0", tail: bool = False):
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

    if tail:
        messages = messages[-20:]

    channel = bot.get_channel(INSPECT_CHANNEL_ID)
    label = f"tail:{len(messages)//2}r" if tail else f"{len(messages)} messages"
    thread = await channel.create_thread(
        name=f"Session {session_id[:8]} — {label}",
        type=discord.ChannelType.public_thread,
    )
    for m in messages:
        await thread.send(format_message(m))

    await interaction.followup.send(f"History opened in {thread.mention}")


# ── session thread helpers ─────────────────────────────────────────────────────

async def get_or_create_session_thread(session: str) -> discord.Thread:
    # Check in-memory cache first
    if session in _session_threads:
        return _session_threads[session]

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
    await _add_notify_users(thread)
    return thread


# ── IPC socket server ──────────────────────────────────────────────────────────

_DEST_LABELS = {"localSettings": "local", "projectSettings": "project",
                "userSettings": "user", "session": "session"}


def _suggestion_label(suggestion: dict, index: int) -> str:
    stype = suggestion.get("type", "")
    dest = _DEST_LABELS.get(suggestion.get("destination", ""), "")
    if stype == "addRules":
        behavior = suggestion.get("behavior", "allow")
        return f"Allow + {behavior} rule ({dest})" if dest else f"Allow + {behavior} rule"
    if stype == "setMode":
        mode = suggestion.get("mode", "?")
        return f"Allow + set mode: {mode}"
    return f"Allow + Option {index + 1}"


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
        suggestions = req.get("permission_suggestions", [])
        tool_name = req.get("tool_name", "")
        tool_input = req.get("tool_input", {})
        thread = await get_or_create_session_thread(session)
        view = discord.ui.View(timeout=None)

        if tool_name == "AskUserQuestion":
            questions = tool_input.get("questions", [])
            _pending_questions[request_id] = {"questions": questions, "answers": {}}
            for idx, q in enumerate(questions):
                opts = q.get("options", [])
                select_opts = [
                    discord.SelectOption(label=o.get("label", str(o)), value=o.get("label", str(o)))
                    for o in opts[:25]
                ]
                multi = q.get("multiSelect", False)
                select = discord.ui.Select(
                    placeholder=q.get("header", q.get("question", f"Question {idx + 1}")),
                    options=select_opts,
                    min_values=1,
                    max_values=len(select_opts) if multi else 1,
                    custom_id=f"askq:{idx}:{request_id}",
                    row=idx,
                )
                view.add_item(select)
            text_btn = discord.ui.Button(
                label="Answer with text",
                style=discord.ButtonStyle.secondary,
                custom_id=f"askq_text:{request_id}",
                row=min(len(questions), 4),
            )
            view.add_item(text_btn)
            submit = discord.ui.Button(
                label="Submit Answers",
                style=discord.ButtonStyle.success,
                custom_id=f"askq_submit:{request_id}",
                row=min(len(questions), 4),
            )
            view.add_item(submit)
        elif tool_name == "ExitPlanMode":
            _pending_tool_input[request_id] = tool_input
            view.add_item(discord.ui.Button(
                label="Approve Plan", style=discord.ButtonStyle.success,
                custom_id=f"approve:{request_id}",
            ))
            view.add_item(discord.ui.Button(
                label="Reject Plan", style=discord.ButtonStyle.danger,
                custom_id=f"deny:{request_id}",
            ))
            view.add_item(discord.ui.Button(
                label="Give Feedback", style=discord.ButtonStyle.secondary,
                custom_id=f"plan_feedback:{request_id}",
            ))
        else:
            view.add_item(discord.ui.Button(
                label="Approve", style=discord.ButtonStyle.success,
                custom_id=f"approve:{request_id}",
            ))
            view.add_item(discord.ui.Button(
                label="Deny", style=discord.ButtonStyle.danger,
                custom_id=f"deny:{request_id}",
            ))
            for i, suggestion in enumerate(suggestions):
                label = _suggestion_label(suggestion, i)
                view.add_item(discord.ui.Button(
                    label=label, style=discord.ButtonStyle.primary,
                    custom_id=f"suggest:{i}:{request_id}",
                ))
            if suggestions:
                _pending_suggestions[request_id] = suggestions

        await thread.send(text, view=view)

        # Poll for decision file
        decision_file = DECISION_DIR / f"{request_id}.json"
        timeout = PLAN_APPROVAL_TIMEOUT if tool_name == "ExitPlanMode" else APPROVAL_TIMEOUT
        deadline = time.monotonic() + timeout
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

        # Cleanup pending state regardless of outcome
        _pending_questions.pop(request_id, None)
        _pending_tool_input.pop(request_id, None)
        _pending_suggestions.pop(request_id, None)

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
    if interaction.type == discord.InteractionType.modal_submit:
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("askq_modal:"):
            request_id = custom_id[len("askq_modal:"):]
            pending = _pending_questions.get(request_id)
            if pending:
                questions = pending["questions"]
                for idx, q in enumerate(questions):
                    field_id = f"askq_field:{idx}"
                    for component_row in interaction.data.get("components", []):
                        for component in component_row.get("components", []):
                            if component.get("custom_id") == field_id:
                                value = component.get("value", "").strip()
                                if value:
                                    pending["answers"][q["question"]] = value
            await interaction.response.send_message("✅ Text answers recorded. Press **Submit Answers** to confirm.", ephemeral=True)
        elif custom_id.startswith("plan_feedback_modal:"):
            request_id = custom_id[len("plan_feedback_modal:"):]
            feedback = ""
            for component_row in interaction.data.get("components", []):
                for component in component_row.get("components", []):
                    if component.get("custom_id") == "plan_feedback_text":
                        feedback = component.get("value", "").strip()
            decision = {"decision": "deny", "reason": feedback or "Rejected via Discord"}
            _pending_tool_input.pop(request_id, None)
            (DECISION_DIR / f"{request_id}.json").write_text(json.dumps(decision))
            await interaction.response.send_message(f"📝 Feedback submitted", ephemeral=True)
        return

    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get("custom_id", "")
    if ":" not in custom_id:
        return
    parts = custom_id.split(":", 2)
    action = parts[0]

    if action == "askq_text":
        request_id = custom_id[len("askq_text:"):]
        pending = _pending_questions.get(request_id)
        if not pending:
            await interaction.response.send_message("Session expired.", ephemeral=True)
            return
        questions = pending["questions"]
        modal = discord.ui.Modal(title="Answer Questions", custom_id=f"askq_modal:{request_id}")
        for idx, q in enumerate(questions[:5]):
            modal.add_item(discord.ui.TextInput(
                label=q.get("question", f"Question {idx + 1}")[:45],
                custom_id=f"askq_field:{idx}",
                style=discord.TextStyle.paragraph,
                required=False,
                placeholder="Leave blank to use your dropdown selection",
            ))
        await interaction.response.send_modal(modal)
        return

    if action == "plan_feedback":
        request_id = custom_id[len("plan_feedback:"):]
        modal = discord.ui.Modal(title="Plan Feedback", custom_id=f"plan_feedback_modal:{request_id}")
        modal.add_item(discord.ui.TextInput(
            label="What should Claude change?",
            custom_id="plan_feedback_text",
            style=discord.TextStyle.paragraph,
            required=True,
            placeholder="Describe what to change or improve in the plan",
        ))
        await interaction.response.send_modal(modal)
        return

    if action == "askq_submit":
        request_id = custom_id[len("askq_submit:"):]
        pending = _pending_questions.get(request_id, {})
        questions = pending.get("questions", [])
        answers = pending.get("answers", {})
        updated_input = {"questions": questions, "answers": answers}
        decision = {"decision": "allow", "updatedInput": updated_input}
        _pending_questions.pop(request_id, None)
        (DECISION_DIR / f"{request_id}.json").write_text(json.dumps(decision))
        await interaction.response.send_message(f"✅ Answers submitted", ephemeral=True)
        return

    if action == "askq":
        # Select menu value update — accumulate into pending answers
        idx = int(parts[1])
        request_id = parts[2]
        pending = _pending_questions.get(request_id)
        if pending:
            questions = pending["questions"]
            if idx < len(questions):
                q_text = questions[idx]["question"]
                values = interaction.data.get("values", [])
                pending["answers"][q_text] = ", ".join(values)
        await interaction.response.send_message(f"✅ Answer recorded", ephemeral=True)
        return

    if action not in ("approve", "deny", "suggest"):
        return

    if action == "approve":
        request_id = custom_id[len("approve:"):]
        # For ExitPlanMode, echo back tool_input as updatedInput
        tool_input = _pending_tool_input.pop(request_id, None)
        if tool_input is not None:
            decision = {"decision": "allow", "updatedInput": tool_input}
        else:
            decision = {"decision": "allow", "reason": "Approved via Discord"}
        await interaction.response.send_message(f"✅ Approved `{request_id}`", ephemeral=True)
    elif action == "deny":
        request_id = custom_id[len("deny:"):]
        _pending_tool_input.pop(request_id, None)
        _pending_questions.pop(request_id, None)
        decision = {"decision": "deny", "reason": "Denied via Discord"}
        await interaction.response.send_message(f"❌ Denied `{request_id}`", ephemeral=True)
    else:  # suggest
        idx = int(parts[1])
        request_id = parts[2]
        suggestions = _pending_suggestions.get(request_id, [])
        if idx < len(suggestions):
            chosen = suggestions[idx]
            decision = {"decision": "allow", "updatedPermissions": [chosen]}
            label = _suggestion_label(chosen, idx)
        else:
            decision = {"decision": "allow"}
            label = f"Option {idx + 1}"
        await interaction.response.send_message(f"✅ {label} `{request_id}`", ephemeral=True)

    _pending_suggestions.pop(request_id, None)
    (DECISION_DIR / f"{request_id}.json").write_text(json.dumps(decision))


@bot.command(name="sync")
@commands.is_owner()
async def cmd_sync(ctx):
    await tree.sync()
    await ctx.send("Slash commands synced.")


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
