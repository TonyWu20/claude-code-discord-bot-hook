#!/usr/bin/env python3
"""
Discord bot for Claude Code session inspection.

Commands:
  /sessions          — list active/recent sessions (ephemeral, inspect channel)
  /history [session] — show conversation history in a thread (inspect channel)
  /summary [date]    — post project usage summary for a date (forum channel)

Required env vars:
  DISCORD_BOT_TOKEN
  DISCORD_CHANNEL_ID        — approvals channel
  DISCORD_INSPECT_CHANNEL_ID — inspection channel (falls back to DISCORD_CHANNEL_ID)
  DISCORD_SUMMARY_CHANNEL_ID — forum channel for summaries (falls back to INSPECT_CHANNEL_ID)
  DISCORD_SYNC_CHANNEL_ID   — forum channel for session sync threads (falls back to SUMMARY_CHANNEL_ID)
  DISCORD_NOTIFY_USER_IDS   — comma-separated user IDs to auto-add to session threads (optional)

The bot handles Approve/Deny button interactions from notify_discord.py
by writing decision files to ~/.claude/discord-decisions/.
"""

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
INSPECT_CHANNEL_ID = int(os.environ.get("DISCORD_INSPECT_CHANNEL_ID", CHANNEL_ID))
SUMMARY_CHANNEL_ID = int(os.environ.get("DISCORD_SUMMARY_CHANNEL_ID", INSPECT_CHANNEL_ID))
SYNC_CHANNEL_ID = int(os.environ.get("DISCORD_SYNC_CHANNEL_ID", SUMMARY_CHANNEL_ID))
APPROVAL_TIMEOUT = int(os.environ.get("DISCORD_APPROVAL_TIMEOUT", "300"))
PLAN_APPROVAL_TIMEOUT = int(os.environ.get("DISCORD_PLAN_APPROVAL_TIMEOUT", "1800"))
_NOTIFY_USER_IDS: list[int] = [
    int(uid.strip())
    for uid in os.environ.get("DISCORD_NOTIFY_USER_IDS", "").split(",")
    if uid.strip()
]
SOCKET_PATH = "/tmp/claude_discord.sock"
DISCORD_BOT_HOST = os.environ.get("DISCORD_BOT_HOST", "")
PID_FILE = "/tmp/claude_discord_bot.pid"
READY_FILE = "/tmp/claude_discord_bot.ready"

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
DECISION_DIR = Path.home() / ".claude" / "discord-decisions"
DECISION_DIR.mkdir(exist_ok=True)
THREAD_CACHE_FILE = Path("/tmp/claude_discord_threads.json")
TMUX_CACHE_FILE = Path("/tmp/claude_discord_tmux.json")
SYNC_STATE_FILE = Path("/tmp/claude_discord_sync.json")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# session_id -> discord.Thread (in-memory cache, stable key)
_session_threads: dict[str, discord.Thread] = {}
# session_label -> sync state (in-memory cache)
_session_sync: dict[str, dict] = {}
# request_id -> list of permission_suggestions entries
_pending_suggestions: dict[str, list] = {}
# request_id -> tool_input (for ExitPlanMode, echoed back as updatedInput)
_pending_tool_input: dict[str, dict] = {}
# request_id -> {questions: [...], answers: {question_text: label(s)}}
_pending_questions: dict[str, dict] = {}


def _build_label_to_session_id_map() -> dict[str, str]:
    """Build {label_or_short_id: session_id} from session JSON files for cache migration."""
    host = socket.gethostname().split(".")[0]
    mapping: dict[str, str] = {}
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            sid = data.get("sessionId", "")
            name = data.get("name", "")
            if sid:
                mapping[f"{host}-{name or sid[:8]}"] = sid
                mapping[sid[:8]] = sid
        except (json.JSONDecodeError, OSError):
            continue
    return mapping


def _load_thread_ids() -> dict[str, int]:
    try:
        raw = json.loads(THREAD_CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    # Migration: remap old label-keyed entries to session_id keys
    label_map = _build_label_to_session_id_map()
    needs_migration = False
    migrated: dict[str, int] = {}
    for key, thread_id in raw.items():
        if key in label_map:
            migrated[label_map[key]] = thread_id
            needs_migration = True
        else:
            migrated[key] = thread_id
    if needs_migration:
        try:
            THREAD_CACHE_FILE.write_text(json.dumps(migrated))
        except OSError:
            pass
    return migrated


def _save_thread_id(session_id: str, thread_id: int) -> None:
    ids = _load_thread_ids()
    ids[session_id] = thread_id
    THREAD_CACHE_FILE.write_text(json.dumps(ids))


async def _add_notify_users(thread: discord.Thread) -> None:
    for uid in _NOTIFY_USER_IDS:
        try:
            await thread.add_user(discord.Object(id=uid))
        except discord.HTTPException as e:
            print(f"[warn] Failed to add user {uid} to thread {thread.id}: {e}")


# ── sync state ─────────────────────────────────────────────────────────────────


def _load_sync_state() -> dict[str, dict]:
    try:
        return json.loads(SYNC_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sync_state(state: dict) -> None:
    try:
        SYNC_STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


# ── session helpers ────────────────────────────────────────────────────────────


def _safe_code_block(content: str, lang: str = "") -> str:
    """Wrap content in a fenced code block, splitting at inner ``` boundaries
    so Discord renders nested code blocks correctly.

    Instead of replacing ``` with ugly fullwidth lookalikes, this closes
    the outer block before each inner fence, lets the inner block render
    natively, then reopens the outer block afterward. No blank lines
    between consecutive blocks.
    """
    if not any(line.strip().startswith("```") for line in content.split("\n")):
        return f"```{lang}\n{content}\n```"

    open_fence = f"```{lang}" if lang else "```"
    segments: list[tuple[str, str]] = []
    lines = content.split("\n")
    i = 0
    buf: list[str] = []

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            if buf:
                segments.append(("text", "\n".join(buf)))
                buf = []
            j = i + 1
            while j < len(lines):
                if lines[j].strip().startswith("```"):
                    break
                j += 1
            if j < len(lines):
                segments.append(("code", "\n".join(lines[i : j + 1])))
                i = j + 1
                continue
            else:
                segments.append(("code", "\n".join(lines[i:])))
                break
        else:
            buf.append(lines[i])
        i += 1

    if buf:
        segments.append(("text", "\n".join(buf)))

    result: list[str] = []
    for seg_type, seg_text in segments:
        if seg_type == "text" and seg_text.strip():
            result.append(seg_text)
        elif seg_type == "code":
            result.append(seg_text)

    return "\n".join(result)


def _find_last_user_message_idx(messages: list[dict]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return -1


def _get_conversation_context(session_id: str) -> str | None:
    if not session_id:
        return None
    conv_file = find_conversation_file(session_id)
    if not conv_file:
        return None
    messages = extract_messages(conv_file)
    if not messages:
        return None
    last_user_idx = _find_last_user_message_idx(messages)
    if last_user_idx < 0:
        return None
    context_msgs = messages[last_user_idx:]
    parts = ["**Context from your last prompt:**"]
    for m in context_msgs:
        role = "\ud83d\udc64 You" if m["role"] == "user" else "\ud83e\udd16 Claude"
        parts.append(f"\n{role}:\n{m['content']}")
    return "".join(parts)


def _in_fence(text: str) -> bool:
    """Return True if ``text`` is inside an open ``` fenced code block."""
    count = 0
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            count += 1
    return count % 2 == 1


def _extract_fence_lang(text: str) -> str:
    """Return the opening fence line (e.g. '```python\n') from text starting with ```."""
    newline = text.find("\n")
    if newline == -1:
        return "```\n"
    return text[: newline + 1]


def split_text(text: str, limit: int = 1990) -> list[str]:
    """Split text into chunks <= limit chars, never breaking inside ``` fences."""
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        if cut > 0 and _in_fence(text[:cut]):
            prev = text.rfind("\n```", 0, cut)
            if prev > 0:
                # Inside a code block — close fence at cut, reopen in next chunk
                fence_header = _extract_fence_lang(text[prev:].lstrip("\n"))
                parts.append(text[:cut] + "\n```")
                text = fence_header + text[cut:].lstrip("\n")
                continue
            elif text.startswith("```"):
                closing = text.find("\n```\n", 4)
                if closing > 0 and closing + 5 <= limit + 200:
                    cut = closing + 5
                else:
                    fence_header = _extract_fence_lang(text)
                    header_len = len(fence_header)
                    inner = text.rfind("\n", header_len, limit - 3)
                    if inner <= header_len:
                        inner = limit - 3
                    parts.append(text[:inner] + "\n```")
                    text = fence_header.rstrip("\n") + "\n" + text[inner:].lstrip("\n")
                    continue
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


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


def discover_tmux_target_for_session(session_label: str) -> str | None:
    """Find tmux pane target for a session, checking cache first then direct discovery.

    Uses `tmux` and `ps` directly instead of psutil for robustness.
    """
    # Check cache first
    try:
        cache = json.loads(TMUX_CACHE_FILE.read_text()) if TMUX_CACHE_FILE.exists() else {}
        if session_label in cache:
            return cache[session_label]
    except (OSError, json.JSONDecodeError):
        pass
    # Walk session JSONs to find the PID for this label
    host = socket.gethostname().split(".")[0]
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            sid = data.get("sessionId", "")
            name = data.get("name", "")
            expected = f"{host}-{name or sid[:8]}"
            if expected != session_label:
                continue
            pid = int(f.stem)
            # Run tmux list-panes to get all pane PIDs and targets
            result = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}:#{window_index}.#{pane_index}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue
            panes: list[tuple[int, str]] = []
            for line in result.stdout.strip().split("\n"):
                if " " in line:
                    ppid_str, target = line.split(" ", 1)
                    try:
                        panes.append((int(ppid_str), target.strip()))
                    except ValueError:
                        continue
            # Walk the ancestor chain using `ps` command (no psutil dependency)
            ancestors = {pid}
            current = pid
            visited = {pid}
            for _ in range(50):  # safety limit
                out = subprocess.run(
                    ["ps", "-o", "ppid=", "-p", str(current)],
                    capture_output=True, text=True, timeout=3,
                )
                if out.returncode != 0 or not out.stdout.strip():
                    break
                ppid_str = out.stdout.strip()
                try:
                    ppid = int(ppid_str)
                except ValueError:
                    break
                if ppid <= 0 or ppid in visited:
                    break
                visited.add(ppid)
                ancestors.add(ppid)
                current = ppid
            for pane_pid, target in panes:
                if pane_pid in ancestors:
                    return target
            return None
        except Exception:
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
                    inp = json.dumps(
                        block.get("input", {}), indent=2, ensure_ascii=False
                    )
                    parts.append(f"🔧 **{name}**\n```json\n{inp.replace('```', '｀｀｀')}\n```")
                elif block.get("type") == "tool_result":
                    result = block.get("content", "")
                    if isinstance(result, list):
                        result = "\n".join(
                            b.get("text", "") for b in result if isinstance(b, dict)
                        )
                    result = str(result).strip()
                    parts.append(f"📤 **Result**\n```\n{result.replace('```', '｀｀｀')}\n```")
                elif block.get("type") == "thinking":
                    thinking = block.get("thinking", "")
                    if thinking:
                        parts.append(f"💭 *{thinking}*")
            content = "\n".join(parts)
        if not content.strip():
            continue
        messages.append(
            {
                "role": role,
                "content": str(content),
                "timestamp": d.get("timestamp", ""),
            }
        )
    return messages


def format_message(m: dict) -> str:
    if m["role"] == "user":
        header = "## 👤 You"
    else:
        header = "## 🤖 Claude"
    content = m["content"]
    return f"{header}\n{content}"


# ── usage summary ──────────────────────────────────────────────────────────────

from datetime import timedelta, timezone


def summarize_usage(date_str: str | None = None) -> dict:
    """Aggregate Claude Code usage by project for a given date.

    Returns dict with:
      date: str — the date queried (YYYY-MM-DD)
      projects: list[dict] — one per project, containing name, duration_ms,
               total_tokens, models breakdown (sorted by total_tokens desc)
    """
    # Parse target date (always UTC)
    if date_str and date_str.lower() != "today":
        target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        target = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = target + timedelta(days=1)
    start_ms = int(target.timestamp() * 1000)
    end_ms = int(day_end.timestamp() * 1000)
    date_prefix = target.strftime("%Y-%m-%d")

    # Step 1: find unique (project, sessionId) pairs on target date
    session_keys: set[tuple[str, str]] = set()
    try:
        for line in HISTORY_FILE.read_text().splitlines():
            entry = json.loads(line)
            ts = entry.get("timestamp", 0)
            if start_ms <= ts < end_ms:
                session_keys.add((entry["project"], entry["sessionId"]))
    except (OSError, json.JSONDecodeError):
        pass

    if not session_keys:
        return {"date": date_prefix, "projects": []}

    # Step 2: for each session, parse JSONL and aggregate by project
    projects: dict[str, dict] = {}

    for project_path, session_id in session_keys:
        jsonl_path = find_conversation_file(session_id)
        if not jsonl_path:
            continue

        proj_name = Path(project_path).name
        if proj_name not in projects:
            projects[proj_name] = {
                "name": proj_name,
                "total_tokens": 0,
                "models": {},
                "session_durations": [],
            }

        proj = projects[proj_name]
        session_timestamps: list[datetime] = []

        for line in jsonl_path.read_text().splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = d.get("timestamp", "")
            if not ts.startswith(date_prefix):
                continue

            # Track timestamps for per-session duration on this date
            if d.get("type") in ("user", "assistant"):
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    session_timestamps.append(t)
                except ValueError:
                    pass

            # Extract per-turn token usage from assistant messages
            if d.get("type") == "assistant":
                msg = d.get("message", {})
                model = msg.get("model", "unknown")
                usage = msg.get("usage", {})

                input_t = usage.get("input_tokens", 0) or 0
                output_t = usage.get("output_tokens", 0) or 0
                cache_read = usage.get("cache_read_input_tokens", 0) or 0
                cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                total = input_t + output_t + cache_read + cache_creation

                if model not in proj["models"]:
                    proj["models"][model] = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read": 0,
                        "cache_creation": 0,
                        "total": 0,
                    }
                m = proj["models"][model]
                m["input_tokens"] += input_t
                m["output_tokens"] += output_t
                m["cache_read"] += cache_read
                m["cache_creation"] += cache_creation
                m["total"] += total
                proj["total_tokens"] += total

        # Compute per-session duration (on this date only)
        if len(session_timestamps) >= 2:
            session_timestamps.sort()
            duration = int(
                (session_timestamps[-1] - session_timestamps[0]).total_seconds()
                * 1000
            )
            proj["session_durations"].append(duration)

    # Step 3: finalize and sort
    result_projects = []
    for proj in projects.values():
        proj["duration_ms"] = sum(proj.pop("session_durations"))
        result_projects.append(proj)

    result_projects.sort(key=lambda p: p["total_tokens"], reverse=True)
    return {"date": date_prefix, "projects": result_projects}


def _format_duration(ms: int) -> str:
    """Format milliseconds to a human-readable duration string."""
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    seconds %= 60
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours = minutes // 60
    minutes %= 60
    if minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h"


def _format_number(n: int) -> str:
    """Format a number with comma separators (e.g. 12345 -> '12,345')."""
    return f"{n:,}"


def _build_summary_text(summary: dict) -> str:
    """Build the formatted Discord message text from a summary dict."""
    lines = [f"📊 **Claude Code Summary — {summary['date']}**\n"]
    projects = summary["projects"]

    if not projects:
        lines.append("No Claude Code activity found for this date.")
        return "\n".join(lines)

    for i, proj in enumerate(projects, 1):
        lines.append(f"**{i}. {proj['name']}**")
        lines.append(f"⏱️ Session time: {_format_duration(proj['duration_ms'])}")
        lines.append(
            f"🔤 Total tokens: {_format_number(proj['total_tokens'])}"
        )

        models = proj.get("models", {})
        if models:
            lines.append("Models:")
            for model_name, m in sorted(
                models.items(), key=lambda x: x[1]["total"], reverse=True
            ):
                total = _format_number(m["total"])
                inp = _format_number(m["input_tokens"])
                out = _format_number(m["output_tokens"])
                lines.append(
                    f"• {model_name}: {total} tokens"
                    f" (in: {inp}, out: {out})"
                )
        lines.append("")  # blank line between projects

    return "\n".join(lines)


# ── slash commands ─────────────────────────────────────────────────────────────


@tree.command(name="sessions", description="List active/recent Claude Code sessions")
async def slash_sessions(interaction: discord.Interaction):
    if interaction.channel_id != INSPECT_CHANNEL_ID:
        await interaction.response.send_message(
            "Use the inspect channel.", ephemeral=True
        )
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
        started = (
            datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            if ts
            else ""
        )
        lines.append(f"`{i}` `{sid}` {started} — `{cwd}`")
    await interaction.response.send_message(
        "**Active/recent sessions:**\n" + "\n".join(lines), ephemeral=True
    )


@tree.command(name="history", description="Show conversation history for a session")
@app_commands.describe(
    session="Session index (default 0) or sessionId prefix",
    tail="Show only last 10 rounds (default False)",
)
async def slash_history(
    interaction: discord.Interaction, session: str = "0", tail: bool = False
):
    if interaction.channel_id != INSPECT_CHANNEL_ID:
        await interaction.response.send_message(
            "Use the inspect channel.", ephemeral=True
        )
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
        await interaction.followup.send(
            f"No conversation file found for session `{session_id[:8]}`."
        )
        return

    messages = extract_messages(conv_file)
    if not messages:
        await interaction.followup.send("No messages in this session yet.")
        return

    if tail:
        messages = messages[-20:]

    channel = bot.get_channel(INSPECT_CHANNEL_ID)
    label = f"tail:{len(messages) // 2}r" if tail else f"{len(messages)} messages"
    thread = await channel.create_thread(
        name=f"Session {session_id[:8]} — {label}",
        type=discord.ChannelType.public_thread,
    )
    for m in messages:
        await thread.send(format_message(m))

    await interaction.followup.send(f"History opened in {thread.mention}")


@tree.command(name="summary", description="Summarize Claude Code usage by project for a date")
@app_commands.describe(
    date="Date in YYYY-MM-DD format (default: today)",
)
async def slash_summary(
    interaction: discord.Interaction, date: str = "today"
):
    await interaction.response.defer(ephemeral=True)

    summary = summarize_usage(date if date != "today" else None)

    # Post summary to the summary channel (forum)
    channel = bot.get_channel(SUMMARY_CHANNEL_ID)
    if not channel:
        await interaction.followup.send(
            "Summary channel not found. Check DISCORD_SUMMARY_CHANNEL_ID.",
        )
        return

    text = _build_summary_text(summary)

    try:
        if isinstance(channel, discord.ForumChannel):
            created = await channel.create_thread(
                name=f"Claude Code Summary — {summary['date']}",
                content=text,
            )
            # ThreadWithMessage is a named tuple (thread, message)
            thread = created.thread if hasattr(created, "thread") else created[0]
        else:
            thread = await channel.create_thread(
                name=f"Claude Code Summary — {summary['date']}",
                type=discord.ChannelType.public_thread,
            )
            await thread.send(text)
        await interaction.followup.send(
            f"Summary posted in {thread.mention}"
        )
    except (discord.HTTPException, TypeError) as e:
        await interaction.followup.send(
            f"Failed to post summary: {e}"
        )


# ── sync helpers ────────────────────────────────────────────────────────────────


def _resolve_session(input_str: str) -> dict | None:
    """Resolve a session by name, sessionId prefix, or hostname-prefixed label."""
    sessions = load_sessions()
    host = socket.gethostname().split(".")[0]
    for s in sessions:
        sid = s.get("sessionId", "")
        name = s.get("name", "")
        label = f"{host}-{name or sid[:8]}"
        if name == input_str or sid.startswith(input_str) or label == input_str:
            return s
    return None


def _resolve_sync_label(session: dict) -> str:
    """Build the sync label (hostname-prefixed) from a session dict."""
    host = socket.gethostname().split(".")[0]
    sid = session.get("sessionId", "")
    name = session.get("name", "")
    return f"{host}-{name or sid[:8]}"


async def _activate_sync(
    interaction: discord.Interaction, session: dict,
) -> None:
    """Create forum post, dump history, store sync state."""
    session_label = _resolve_sync_label(session)
    session_id = session.get("sessionId", "")

    if _session_sync.get(session_label, {}).get("synced"):
        await interaction.followup.send(
            f"`{session_label}` is already synced. Use `/sync {session_label} off` first.",
            ephemeral=True,
        )
        return

    # Discover tmux target — check cache, then fallback
    tmux_target = _session_sync.get(session_label, {}).get("tmux_target", "")
    if not tmux_target:
        tmux_target = discover_tmux_target_for_session(session_label) or ""

    # Create forum post
    channel = bot.get_channel(SYNC_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("Sync channel not found. Check DISCORD_SYNC_CHANNEL_ID.", ephemeral=True)
        return

    # Build initial content from conversation history, paginated
    conv_file = find_conversation_file(session_id)
    chunks: list[str] = []
    last_synced_line = 0
    if conv_file:
        messages = extract_messages(conv_file)
        if messages:
            last_synced_line = len(messages)
            for m in messages:
                msg_text = format_message(m)
                if not chunks or len(chunks[-1]) + len(msg_text) + 2 > 1900:
                    chunks.append(msg_text)
                else:
                    chunks[-1] += "\n\n" + msg_text
    if not chunks:
        chunks = ["**Session sync started — type in this thread to send prompts to Claude.**"]

    # Split first chunk if it exceeds Discord's limit
    first_parts = split_text(chunks[0])
    first_content = first_parts[0]
    overflow = first_parts[1:] + chunks[1:] if len(chunks) > 1 else first_parts[1:]

    try:
        created = await channel.create_thread(
            name=f"Sync: {session_label}",
            content=first_content,
        )
        # Forum channels return ThreadWithMessage (thread, message); regular channels return Thread
        thread = created.thread if hasattr(created, "thread") else created
    except (discord.HTTPException, TypeError) as e:
        await interaction.followup.send(f"Failed to create forum post: {e}", ephemeral=True)
        return

    # Post remaining chunks as replies
    for chunk in overflow:
        try:
            if len(chunk) > 1950:
                for i in range(0, len(chunk), 1950):
                    await thread.send(chunk[i:i+1950])
            else:
                await thread.send(chunk)
        except discord.HTTPException:
            break

    # Store sync state
    _session_sync[session_label] = {
        "synced": True,
        "tmux_target": tmux_target,
        "forum_thread_id": thread.id,
        "forum_channel_id": SYNC_CHANNEL_ID,
        "last_synced_line": last_synced_line,
    }
    _save_sync_state(_session_sync)

    try:
        for uid in _NOTIFY_USER_IDS:
            await thread.add_user(discord.Object(id=uid))
    except discord.HTTPException:
        pass

    await interaction.followup.send(
        f"Syncing `{session_label}` → {thread.mention}. Type in that thread to send prompts to Claude.",
        ephemeral=True,
    )


async def _deactivate_sync(interaction: discord.Interaction, session_label: str) -> None:
    """Turn off sync for a session."""
    state = _session_sync.get(session_label)
    if not state or not state.get("synced"):
        await interaction.followup.send(
            f"`{session_label}` is not currently synced.", ephemeral=True,
        )
        return

    state["synced"] = False
    _save_sync_state(_session_sync)

    # Post final message to forum thread
    try:
        thread_id = state.get("forum_thread_id")
        if thread_id:
            channel = bot.get_channel(state.get("forum_channel_id", 0)) or bot.get_channel(SYNC_CHANNEL_ID)
            if channel:
                thread = await bot.fetch_channel(thread_id)
                await thread.send("**Sync disabled** — this thread will no longer receive updates.")
    except (discord.HTTPException, discord.NotFound):
        pass

    await interaction.followup.send(
        f"Sync disabled for `{session_label}`.", ephemeral=True,
    )


class SyncSessionSelect(discord.ui.View):
    """Dropdown that starts or stops sync on a session."""

    def __init__(self, sessions: list[dict], is_off: bool = False):
        super().__init__(timeout=60)
        self.is_off = is_off
        host = socket.gethostname().split(".")[0]
        options = []
        for s in sessions[:25]:
            sid = s.get("sessionId", "")
            name = s.get("name", "") or sid[:8]
            label = f"{host}-{name}"[:100]
            cwd = s.get("cwd", "?")
            options.append(discord.SelectOption(
                label=label, value=sid, description=cwd[:100],
            ))
        if not options:
            self.select = discord.ui.Select(
                placeholder="No sessions available",
                options=[discord.SelectOption(label="None available", value="")],
                disabled=True,
            )
        else:
            self.select = discord.ui.Select(
                placeholder="Select a session",
                options=options,
            )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        session_id = self.select.values[0]
        if not session_id:
            await interaction.response.edit_message(content="No session selected.", view=None)
            return

        session = _resolve_session(session_id)
        if not session:
            await interaction.response.edit_message(
                content=f"Session `{session_id[:8]}` not found.", view=None,
            )
            return

        session_label = _resolve_sync_label(session)
        await interaction.response.edit_message(
            content=f"Selected `{session_label}`...", view=None,
        )

        if self.is_off:
            await _deactivate_sync(interaction, session_label)
        else:
            await _activate_sync(interaction, session)


class SyncToggleView(discord.ui.View):
    """Two-button view: sync on / off for a direct-match session."""

    def __init__(self, session_label: str, session_id: str):
        super().__init__(timeout=30)
        self.session_label = session_label
        self.session_id = session_id
        self.add_item(discord.ui.Button(
            label="Sync ON", style=discord.ButtonStyle.success,
            custom_id=f"sync_on:{session_label}",
        ))
        self.add_item(discord.ui.Button(
            label="Sync OFF", style=discord.ButtonStyle.danger,
            custom_id=f"sync_off:{session_label}",
        ))


@tree.command(name="sync", description="Sync a session to a forum thread for away-from-desk control")
@app_commands.describe(
    session="Session name, sessionId prefix, or type 'off' to list synced sessions",
)
async def slash_sync(
    interaction: discord.Interaction, session: str | None = None,
):
    if interaction.channel_id != INSPECT_CHANNEL_ID:
        await interaction.response.send_message("Use the inspect channel.", ephemeral=True)
        return

    # /sync off — list synced sessions to pick one to stop
    if session == "off":
        synced = [(label, st) for label, st in _session_sync.items() if st.get("synced")]
        if not synced:
            await interaction.response.send_message("No sessions are currently synced.", ephemeral=True)
            return
        # Build session dicts for SyncSessionSelect
        sessions = []
        for label, _ in synced:
            s = _resolve_session(label)
            if s:
                sessions.append(s)
        if not sessions:
            await interaction.response.send_message("Could not resolve synced sessions.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select a session to stop syncing:", view=SyncSessionSelect(sessions, is_off=True), ephemeral=True,
        )
        return

    # /sync <name> — direct match and activate
    if session:
        s = _resolve_session(session)
        if not s:
            await interaction.response.send_message(
                f"Session `{session}` not found. Use `/sync` to see active sessions.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await _activate_sync(interaction, s)
        return

    # /sync (no args) — show Select of active sessions
    await interaction.response.defer(ephemeral=True)
    sessions = load_sessions()
    active = []
    for s in sessions:
        pid = s.get("_pid", "")
        if pid:
            try:
                os.kill(int(pid), 0)
                active.append(s)
            except (OSError, ValueError):
                continue
    if not active:
        await interaction.followup.send(
            "No active Claude Code sessions found. Start one in tmux first.", ephemeral=True,
        )
        return

    await interaction.followup.send(
        "Select a session to sync away from your desk:",
        view=SyncSessionSelect(active), ephemeral=True,
    )


# ── session thread helpers ─────────────────────────────────────────────────────


async def get_or_create_session_thread(
    session_id: str, session_label: str
) -> discord.Thread:
    # Use session_id as the stable cache key
    if session_id in _session_threads:
        thread = _session_threads[session_id]
        # Detect rename: check if thread name matches current label
        expected_name = f"Session {session_label}"
        if thread.name != expected_name:
            try:
                await thread.edit(name=expected_name)
            except discord.HTTPException:
                pass
        return thread

    # Check persisted thread IDs (keyed by session_id after migration)
    thread_ids = _load_thread_ids()
    if session_id in thread_ids:
        channel = bot.get_channel(CHANNEL_ID)
        try:
            thread = await bot.fetch_channel(thread_ids[session_id])
            # Fix name if out of date (e.g. due to rename)
            expected_name = f"Session {session_label}"
            if thread.name != expected_name:
                try:
                    await thread.edit(name=expected_name)
                except discord.HTTPException:
                    pass
            _session_threads[session_id] = thread
            return thread
        except (discord.NotFound, discord.HTTPException):
            pass

    # Create new thread
    channel = bot.get_channel(CHANNEL_ID)
    thread = await channel.create_thread(
        name=f"Session {session_label}",
        type=discord.ChannelType.public_thread,
    )
    _session_threads[session_id] = thread
    _save_thread_id(session_id, thread.id)
    await _add_notify_users(thread)
    return thread


# ── IPC socket server ──────────────────────────────────────────────────────────

_DEST_LABELS = {
    "localSettings": "local",
    "projectSettings": "project",
    "userSettings": "user",
    "session": "session",
}


def _suggestion_label(suggestion: dict, index: int) -> str:
    stype = suggestion.get("type", "")
    dest = _DEST_LABELS.get(suggestion.get("destination", ""), "")
    if stype == "addRules":
        behavior = suggestion.get("behavior", "allow")
        return (
            f"Allow + {behavior} rule ({dest})" if dest else f"Allow + {behavior} rule"
        )
    if stype == "setMode":
        mode = suggestion.get("mode", "?")
        return f"Allow + set mode: {mode}"
    return f"Allow + Option {index + 1}"


async def handle_ipc_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        req = json.loads(line)
    except Exception:
        writer.close()
        return

    msg_type = req.get("type")
    session = req.get("session", "unknown")  # display label
    session_id = req.get("session_id", "")  # stable key (may be empty in legacy IPC)
    tmux_target = req.get("tmux_target", "")  # tmux target for this session
    text = req.get("text", "")

    # Store tmux_target in sync state if provided
    if tmux_target:
        if session not in _session_sync:
            _session_sync[session] = {"synced": False}
        _session_sync[session]["tmux_target"] = tmux_target
        _save_sync_state(_session_sync)

    if bot.get_channel(CHANNEL_ID) is None:
        writer.write(b'{"ok": false, "error": "bot not connected"}\n')
        await writer.drain()
        writer.close()
        return

    if msg_type == "notify":
        thread = await get_or_create_session_thread(session_id or session, session)
        await thread.send(text)
        writer.write(b'{"ok": true}\n')

        # Phase 5: if synced and this is a Stop output, update forum with new messages
        state = _session_sync.get(session)
        if state and state.get("synced") and text.startswith("**Claude:**"):
            forum_thread_id = state.get("forum_thread_id")
            if forum_thread_id:
                try:
                    conv_file = find_conversation_file(session_id)
                    if conv_file:
                        messages = extract_messages(conv_file)
                        new_count = len(messages) - state.get("last_synced_line", 0)
                        if new_count > 0:
                            new_msgs = messages[-new_count:]
                            # Build paginated chunks for the new messages
                            new_chunks: list[str] = []
                            for m in new_msgs:
                                msg_text = format_message(m)
                                if not new_chunks or len(new_chunks[-1]) + len(msg_text) + 2 > 1900:
                                    new_chunks.append(msg_text)
                                else:
                                    new_chunks[-1] += "\n\n" + msg_text
                            try:
                                forum_chan = bot.get_channel(state.get("forum_channel_id", 0))
                                if forum_chan:
                                    forum_thread = await bot.fetch_channel(forum_thread_id)
                                    for chunk in new_chunks:
                                        await forum_thread.send(chunk)
                            except (discord.NotFound, discord.HTTPException):
                                pass
                            state["last_synced_line"] = len(messages)
                            _save_sync_state(_session_sync)
                except Exception:
                    pass

    elif msg_type == "approve":
        request_id = req.get("request_id", "")
        suggestions = req.get("permission_suggestions", [])
        tool_name = req.get("tool_name", "")
        tool_input = req.get("tool_input", {})
        thread = await get_or_create_session_thread(session_id or session, session)
        view = discord.ui.View(timeout=None)

        if tool_name == "AskUserQuestion":
            questions = tool_input.get("questions", [])
            _pending_questions[request_id] = {"questions": questions, "answers": {}}
            for idx, q in enumerate(questions):
                opts = q.get("options", [])
                select_opts = [
                    discord.SelectOption(
                        label=o.get("label", str(o)), value=o.get("label", str(o))
                    )
                    for o in opts[:25]
                ]
                multi = q.get("multiSelect", False)
                select = discord.ui.Select(
                    placeholder=q.get(
                        "header", q.get("question", f"Question {idx + 1}")
                    ),
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
            view.add_item(
                discord.ui.Button(
                    label="Approve Plan",
                    style=discord.ButtonStyle.success,
                    custom_id=f"approve:{request_id}",
                )
            )
            view.add_item(
                discord.ui.Button(
                    label="Reject Plan",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"deny:{request_id}",
                )
            )
            view.add_item(
                discord.ui.Button(
                    label="Give Feedback",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"plan_feedback:{request_id}",
                )
            )
        else:
            # Row 0: Approve + Deny
            view.add_item(
                discord.ui.Button(
                    label="Approve",
                    style=discord.ButtonStyle.success,
                    custom_id=f"approve:{request_id}",
                    row=0,
                )
            )
            view.add_item(
                discord.ui.Button(
                    label="Deny",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"deny:{request_id}",
                    row=0,
                )
            )
            # Rows 1-4: suggestion buttons (max 4 to stay within 5 row limit)
            for i, suggestion in enumerate(suggestions[:4]):
                row = i + 1
                label = _suggestion_label(suggestion, i)
                view.add_item(
                    discord.ui.Button(
                        label=label,
                        style=discord.ButtonStyle.primary,
                        custom_id=f"suggest:{i}:{request_id}",
                        row=row,
                    )
                )
                # Add Edit Rule button for addRules suggestions with ruleContent
                if suggestion.get("type") == "addRules":
                    rules = suggestion.get("rules", [])
                    if rules and "ruleContent" in rules[0]:
                        view.add_item(
                            discord.ui.Button(
                                label="Edit Rule",
                                style=discord.ButtonStyle.secondary,
                                custom_id=f"edit_rule:{i}:{request_id}",
                                row=row,
                            )
                        )
            if suggestions:
                _pending_suggestions[request_id] = suggestions

        # Send conversation context before the approval message
        if session_id:
            context = _get_conversation_context(session_id)
            if context:
                for chunk in split_text(context):
                    await thread.send(chunk)

        await thread.send(text, view=view)

        # Forward a notification to the synced forum thread
        forum_state = _session_sync.get(session)
        if forum_state and forum_state.get("synced"):
            ftid = forum_state.get("forum_thread_id")
            if ftid:
                try:
                    fchan = bot.get_channel(forum_state.get("forum_channel_id", 0))
                    if fchan:
                        fthread = await bot.fetch_channel(ftid)
                        if session_id:
                            forum_context = _get_conversation_context(session_id)
                            if forum_context:
                                for chunk in split_text(forum_context):
                                    await fthread.send(chunk)
                        for chunk in split_text(text):
                            await fthread.send(chunk)
                except (discord.NotFound, discord.HTTPException):
                    pass

        # Poll for decision file
        decision_file = DECISION_DIR / f"{request_id}.json"
        timeout = (
            PLAN_APPROVAL_TIMEOUT if tool_name == "ExitPlanMode" else APPROVAL_TIMEOUT
        )
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
            result = {
                "decision": "ask",
                "reason": "Timed out waiting for Discord response",
            }
        writer.write((json.dumps(result) + "\n").encode())

    else:
        writer.write(b'{"ok": false}\n')

    await writer.drain()
    writer.close()


async def run_socket_server() -> None:
    if DISCORD_BOT_HOST:
        host, port = DISCORD_BOT_HOST.split(":", 1)
        server = await asyncio.start_server(handle_ipc_client, host=host, port=int(port))
    else:
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
            request_id = custom_id[len("askq_modal:") :]
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
            await interaction.response.send_message(
                "✅ Text answers recorded. Press **Submit Answers** to confirm.",
                ephemeral=True,
            )
        elif custom_id.startswith("plan_feedback_modal:"):
            request_id = custom_id[len("plan_feedback_modal:") :]
            feedback = ""
            for component_row in interaction.data.get("components", []):
                for component in component_row.get("components", []):
                    if component.get("custom_id") == "plan_feedback_text":
                        feedback = component.get("value", "").strip()
            decision = {
                "decision": "deny",
                "reason": feedback or "Rejected via Discord",
            }
            _pending_tool_input.pop(request_id, None)
            (DECISION_DIR / f"{request_id}.json").write_text(json.dumps(decision))
            await interaction.response.send_message(
                f"📝 Feedback submitted", ephemeral=True
            )
        elif custom_id.startswith("edit_rule_modal:"):
            rest = custom_id[len("edit_rule_modal:"):]
            idx = int(rest.split(":", 1)[0])
            request_id = rest.split(":", 1)[1]
            rule_content = ""
            for component_row in interaction.data.get("components", []):
                for component in component_row.get("components", []):
                    if component.get("custom_id") == "edit_rule_text":
                        rule_content = component.get("value", "").strip()
            suggestions = _pending_suggestions.get(request_id, [])
            if idx < len(suggestions):
                suggestion = json.loads(json.dumps(suggestions[idx]))
                rules = suggestion.get("rules", [])
                if rules:
                    rules[0]["ruleContent"] = rule_content
                decision = {"decision": "allow", "updatedPermissions": [suggestion]}
                _pending_suggestions.pop(request_id, None)
                (DECISION_DIR / f"{request_id}.json").write_text(json.dumps(decision))
            await interaction.response.send_message(
                f"✅ Rule updated", ephemeral=True
            )
        return

    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get("custom_id", "")
    if ":" not in custom_id:
        return
    parts = custom_id.split(":", 2)
    action = parts[0]

    if action == "askq_text":
        request_id = custom_id[len("askq_text:") :]
        pending = _pending_questions.get(request_id)
        if not pending:
            await interaction.response.send_message("Session expired.", ephemeral=True)
            return
        questions = pending["questions"]
        modal = discord.ui.Modal(
            title="Answer Questions", custom_id=f"askq_modal:{request_id}"
        )
        for idx, q in enumerate(questions[:5]):
            modal.add_item(
                discord.ui.TextInput(
                    label=q.get("question", f"Question {idx + 1}")[:45],
                    custom_id=f"askq_field:{idx}",
                    style=discord.TextStyle.paragraph,
                    required=False,
                    placeholder="Leave blank to use your dropdown selection",
                )
            )
        await interaction.response.send_modal(modal)
        return

    if action == "plan_feedback":
        request_id = custom_id[len("plan_feedback:") :]
        modal = discord.ui.Modal(
            title="Plan Feedback", custom_id=f"plan_feedback_modal:{request_id}"
        )
        modal.add_item(
            discord.ui.TextInput(
                label="What should Claude change?",
                custom_id="plan_feedback_text",
                style=discord.TextStyle.paragraph,
                required=True,
                placeholder="Describe what to change or improve in the plan",
            )
        )
        await interaction.response.send_modal(modal)
        return

    if action == "edit_rule":
        idx = int(parts[1])
        request_id = parts[2]
        suggestions = _pending_suggestions.get(request_id, [])
        if idx < len(suggestions):
            suggestion = suggestions[idx]
            rules = suggestion.get("rules", [])
            if rules and "ruleContent" in rules[0]:
                current = rules[0]["ruleContent"]
                modal = discord.ui.Modal(
                    title="Edit Rule", custom_id=f"edit_rule_modal:{idx}:{request_id}"
                )
                modal.add_item(
                    discord.ui.TextInput(
                        label="Rule Content",
                        custom_id="edit_rule_text",
                        style=discord.TextStyle.long,
                        default=current,
                        required=False,
                        placeholder="Leave empty for any match",
                    )
                )
                await interaction.response.send_modal(modal)
                return
        await interaction.response.send_message("Suggestion expired.", ephemeral=True)
        return

    if action == "askq_submit":
        request_id = custom_id[len("askq_submit:") :]
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
        request_id = custom_id[len("approve:") :]
        # For ExitPlanMode, echo back tool_input as updatedInput
        tool_input = _pending_tool_input.pop(request_id, None)
        if tool_input is not None:
            decision = {"decision": "allow", "updatedInput": tool_input}
        else:
            decision = {"decision": "allow", "reason": "Approved via Discord"}
        await interaction.response.send_message(
            f"✅ Approved `{request_id}`", ephemeral=True
        )
    elif action == "deny":
        request_id = custom_id[len("deny:") :]
        _pending_tool_input.pop(request_id, None)
        _pending_questions.pop(request_id, None)
        decision = {"decision": "deny", "reason": "Denied via Discord"}
        await interaction.response.send_message(
            f"❌ Denied `{request_id}`", ephemeral=True
        )
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
        await interaction.response.send_message(
            f"✅ {label} `{request_id}`", ephemeral=True
        )

    _pending_suggestions.pop(request_id, None)
    (DECISION_DIR / f"{request_id}.json").write_text(json.dumps(decision))


@bot.command(name="sync")
@commands.is_owner()
async def cmd_sync(ctx):
    await tree.sync()
    await ctx.send("Slash commands synced.")


async def _confirm_message_submitted(
    session_label: str, tmux_target: str, reply_to: discord.Message,
) -> None:
    """Poll the JSONL file until the message appears (Claude processed it),
    retrying Enter if needed. Adds ✅ reaction only once confirmed."""
    sess = _resolve_session(session_label)
    if not sess:
        print(f"[sync]  confirm: no session for label={session_label}")
        try:
            await reply_to.add_reaction("✅")
        except discord.HTTPException:
            pass
        return
    sid = sess.get("sessionId", "")
    if not sid:
        print(f"[sync]  confirm: no sessionId for label={session_label}")
        try:
            await reply_to.add_reaction("✅")
        except discord.HTTPException:
            pass
        return
    conv = find_conversation_file(sid)
    if not conv:
        print(f"[sync]  confirm: no conv file for sid={sid[:8]}")
        try:
            await reply_to.add_reaction("✅")
        except discord.HTTPException:
            pass
        return
    initial = len(conv.read_text().splitlines())
    print(f"[sync]  confirm: watching {conv.name} (initial={initial})")
    await asyncio.sleep(3)
    for attempt in range(5):
        current = len(conv.read_text().splitlines())
        if current > initial:
            print(f"[sync]  confirm: submitted (lines {initial}→{current}) ✅")
            try:
                await reply_to.add_reaction("✅")
            except discord.HTTPException:
                pass
            return
        if attempt < 4:
            print(f"[sync]  confirm: retry Enter {attempt + 1} (still {current} lines)")
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_target, "Enter"], timeout=5,
            )
            await asyncio.sleep(2)
    print(f"[sync]  confirm: gave up after 4 retries ⚠️")
    try:
        await reply_to.add_reaction("⚠️")
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message):
    print(f"[msg] #{message.channel.id} {message.author}: {message.content!r}")
    await bot.process_commands(message)

    # Phase 6: forward messages from synced forum threads to tmux
    if message.author == bot.user:
        return
    if message.content.startswith("/"):
        return
    print(f"[sync] _session_sync keys: {list(_session_sync.keys())}")
    print(f"[sync] channel.id={message.channel.id}, type={type(message.channel)}")
    for _label, state in _session_sync.items():
        ftid = state.get("forum_thread_id")
        print(f"[sync]  checking label={_label} forum_thread_id={ftid} synced={state.get('synced')}")
        if state.get("synced") and ftid == message.channel.id:
            tmux_target = state.get("tmux_target", "")
            print(f"[sync]  MATCH! tmux_target={tmux_target!r}")
            if not tmux_target:
                print(f"[sync]  attempting on-demand discovery for label={_label}")
                try:
                    tmux_target = discover_tmux_target_for_session(_label) or ""
                except Exception as e:
                    print(f"[sync]  discovery exception: {e}")
                    tmux_target = ""
                if tmux_target:
                    state["tmux_target"] = tmux_target
                    _save_sync_state(_session_sync)
                    print(f"[sync]  discovered tmux_target={tmux_target!r} on demand")
                else:
                    try:
                        await message.add_reaction("⚠️")
                    except discord.HTTPException:
                        pass
                    return
            success = send_keys_to_tmux(tmux_target, message.content)
            if success:
                asyncio.create_task(
                    _confirm_message_submitted(_label, tmux_target, message)
                )
            else:
                await message.reply(
                    "Failed to send to tmux — session may have ended. Use `/sync off` to stop sync.",
                    mention_author=True,
                )
            break
    else:
        print(f"[sync] no match found")


def send_keys_to_tmux(target: str, text: str) -> bool:
    """Inject text into a tmux pane. Returns True on success.

    Newlines are sent as C-j (Ctrl+J) so they become literal line breaks
    in the TUI input widget rather than submissions. A final Enter
    submits the complete multiline text as one prompt.
    """
    try:
        args = ["tmux", "send-keys", "-t", target]
        for i, part in enumerate(text.split("\n")):
            if i > 0:
                args.append("C-j")
            args.append(part)
        args.append("Enter")
        result = subprocess.run(args, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


@bot.event
async def on_ready():
    global _session_sync
    print(f"Discord bot ready as {bot.user}")
    _session_sync = _load_sync_state()
    asyncio.create_task(run_socket_server())


if __name__ == "__main__":
    # OS-level file lock — atomic across processes, no race.
    import fcntl
    lock_path = Path("/tmp/claude_discord.lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        print("Another bot instance is running. Exiting.")
        os.close(lock_fd)
        raise SystemExit(0)
    # Release lock on exit (flock auto-releases when fd is closed)
    try:
        bot.run(BOT_TOKEN)
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
