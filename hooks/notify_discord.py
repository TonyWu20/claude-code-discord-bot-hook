#!/usr/bin/env python3
"""
Discord hook for Claude Code. Delegates to the persistent discord_bot.py via Unix socket.

Required env vars:
  DISCORD_BOT_TOKEN   — bot token
  DISCORD_CHANNEL_ID  — channel ID

Optional env vars:
  DISCORD_APPROVAL_TIMEOUT — seconds to wait (default: 120)
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")
SOCKET_PATH = "/tmp/claude_discord.sock"
PID_FILE = "/tmp/claude_discord_bot.pid"
READY_FILE = "/tmp/claude_discord_bot.ready"
VENV_PYTHON = str(Path(__file__).parent / ".venv/bin/python")
BOT_SCRIPT = str(Path(__file__).parent / "discord_bot.py")


def ensure_bot_running() -> None:
    pid_path = Path(PID_FILE)
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text())
            os.kill(pid, 0)  # check alive
            if Path(READY_FILE).exists():
                return  # alive and Discord-connected
            # alive but not yet ready — wait for it
            for _ in range(60):
                if Path(READY_FILE).exists():
                    return
                time.sleep(0.5)
            return  # give up waiting, proceed anyway
        except (OSError, ValueError):
            pid_path.unlink(missing_ok=True)
    subprocess.Popen(
        [VENV_PYTHON, BOT_SCRIPT],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "DISCORD_BOT_TOKEN": BOT_TOKEN, "DISCORD_CHANNEL_ID": CHANNEL_ID},
    )
    # Wait for bot to be fully ready (Discord connected), up to 30 s
    for _ in range(60):
        if Path(READY_FILE).exists():
            return
        time.sleep(0.5)


def ipc(req: dict) -> Optional[dict]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(int(os.environ.get("DISCORD_APPROVAL_TIMEOUT", "120")) + 5)
            s.connect(SOCKET_PATH)
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
        line = buf.split(b"\n")[0].strip()
        return json.loads(line) if line else None
    except Exception:
        return None


def hook_output(decision: str, reason: str = "", event: str = "PermissionRequest") -> None:
    if event == "PermissionRequest":
        behavior = "allow" if decision == "allow" else "deny"
        print(json.dumps({"decision": {"behavior": behavior, "reason": reason}}))
    else:
        # Legacy PreToolUse format
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }))


def resolve_session_label(session_id: str) -> str:
    sessions_dir = Path.home() / ".claude" / "sessions"
    try:
        pid_file = sessions_dir / f"{os.getppid()}.json"
        if pid_file.exists():
            data = json.loads(pid_file.read_text())
            return data.get("name") or session_id[:8]
        for f in sessions_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("sessionId") == session_id:
                    return data.get("name") or session_id[:8]
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return session_id[:8]


def to_yaml(obj: object, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return ""
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.append(to_yaml(v, indent + 1))
            elif isinstance(v, str) and "\n" in v:
                lines.append(f"{pad}{k}: |")
                for line in v.splitlines():
                    lines.append(f"{pad}  {line}")
            else:
                lines.append(f"{pad}{k}: {v}")
        return "\n".join(lines)
    elif isinstance(obj, list):
        if not obj:
            return "[]"
        lines = []
        for item in obj:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(to_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}- {item}")
        return "\n".join(lines)
    else:
        return f"{pad}{obj}"


def main() -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        sys.exit(0)

    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    event = data.get("hook_event_name", "")
    session_id = data.get("session_id", "")
    session_label = resolve_session_label(session_id)

    ensure_bot_running()

    if event == "Stop":
        last_text = data.get("last_assistant_message", "")
        if last_text:
            short = last_text[:1800] + ("..." if len(last_text) > 1800 else "")
            ipc({"type": "notify", "text": f"**Claude:**\n{short}", "session": session_label})
        sys.exit(0)

    if event == "SubagentStop":
        last_text = data.get("last_assistant_message", "")
        agent_type = data.get("agent_type", "subagent")
        if last_text:
            short = last_text[:1800] + ("..." if len(last_text) > 1800 else "")
            ipc({"type": "notify", "text": f"**Claude [{agent_type}]:**\n{short}", "session": session_label})
        sys.exit(0)

    if event not in ("PreToolUse", "PermissionRequest"):
        sys.exit(0)

    # Check for stop flag before processing tool approval
    flag_path = Path(f"/tmp/claude_stop_{session_label}.txt")
    if flag_path.exists():
        reason = flag_path.read_text().strip() or "Stopped via Discord"
        flag_path.unlink(missing_ok=True)
        hook_output("deny", reason, event)
        sys.exit(0)

    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool == "Bash":
        cmd = tool_input.get("command", "")
        short_cmd = cmd[:1800] + ("..." if len(cmd) > 1800 else "")
        msg_text = (
            f"**Claude Code: Approve?**\n\n"
            f"Tool: `Bash`\nCommand:\n```\n{short_cmd}\n```\n"
            f"Session: `{session_label}`"
        )
    else:
        yaml_input = to_yaml(tool_input)
        if len(yaml_input) > 1800:
            yaml_input = yaml_input[:1800] + "\n..."
        msg_text = (
            f"**Claude Code: Approve?**\n\n"
            f"Tool: `{tool}`\nInput:\n```\n{yaml_input}\n```\n"
            f"Session: `{session_label}`"
        )

    request_id = f"{session_label}:{int(time.time())}"
    msg_text += f"\nID: `{request_id}`"

    result = ipc({"type": "approve", "request_id": request_id, "text": msg_text, "session": session_label})
    if result:
        decision = result["decision"]
        reason = result.get("reason", "")
        if decision == "ask":
            # Bot unreachable or timed out — fall through to local prompt
            if event == "PermissionRequest":
                # No output = defer to default behavior
                pass
            else:
                hook_output("ask", reason, event)
        else:
            hook_output(decision, reason, event)
    # No result at all: exit silently so Claude decides locally


if __name__ == "__main__":
    main()
