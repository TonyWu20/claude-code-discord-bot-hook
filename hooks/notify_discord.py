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
        env={
            **os.environ,
            "DISCORD_BOT_TOKEN": BOT_TOKEN,
            "DISCORD_CHANNEL_ID": CHANNEL_ID,
        },
    )
    # Wait for bot to be fully ready (Discord connected), up to 30 s
    for _ in range(60):
        if Path(READY_FILE).exists():
            return
        time.sleep(0.5)


def ipc(req: dict, timeout: int | None = None) -> Optional[dict]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            if timeout is None:
                timeout = int(os.environ.get("DISCORD_APPROVAL_TIMEOUT", "120")) + 5
            s.settimeout(timeout)
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


def ipc_notify_parts(parts: list[str], session: str) -> None:
    """Send multiple text parts as sequential notify messages."""
    for part in parts:
        ipc({"type": "notify", "text": part, "session": session})


def hook_output(
    decision: str,
    reason: str = "",
    event: str = "PermissionRequest",
    updated_permissions: Optional[list] = None,
    updated_input: Optional[dict] = None,
) -> None:
    if event == "PermissionRequest":
        behavior = "allow" if decision == "allow" else "deny"
        d: dict = {"behavior": behavior}
        if reason:
            if behavior == "deny":
                d["message"] = reason
            else:
                d["reason"] = reason
        if behavior == "allow" and updated_permissions:
            d["updatedPermissions"] = updated_permissions
        if updated_input is not None:
            d["updatedInput"] = updated_input
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "decision": d,
                    }
                }
            )
        )
    else:
        # PreToolUse format
        hs: dict = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
        if updated_input is not None:
            hs["updatedInput"] = updated_input
        print(json.dumps({"hookSpecificOutput": hs}))


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


def split_text(text: str, limit: int = 1990) -> list[str]:
    """Split text into chunks <= limit chars, breaking on newlines where possible."""
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        # Try to break at the last newline within the limit
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


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


def _wrap_plan_for_discord(plan: str) -> str:
    """Split plan content at its own ``` fence boundaries and emit alternating
    ```markdown / ```lang blocks so inner code blocks don't break the outer
    formatting.  Each block is properly closed before the next one opens."""
    lines = plan.split("\n")
    result: list[str] = []
    in_fence = False
    md_buf: list[str] = []

    def flush_md() -> None:
        # Strip leading blank lines (avoids empty blocks after a code fence close)
        while md_buf and not md_buf[0].strip():
            md_buf.pop(0)
        if md_buf:
            result.append("```markdown")
            result.extend(md_buf)
            result.append("```")
        md_buf.clear()

    for line in lines:
        st = line.strip()
        if st.startswith("```") and not in_fence:
            flush_md()
            in_fence = True
            lang = st[3:].strip()
            result.append(f"```{lang}")
        elif st == "```" and in_fence:
            result.append("```")
            in_fence = False
        else:
            if in_fence:
                result.append(line)
            else:
                md_buf.append(line)

    flush_md()
    while result and result[-1] == "":
        result.pop()
    return "\n".join(result)


def _sanitize_fences(text: str) -> str:
    """Replace triple backticks with fullwidth lookalikes (｀｀｀) so content
    containing ``` doesn't break outer Discord code block fencing."""
    return text.replace("```", "\uff40\uff40\uff40")


def main() -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        sys.exit(0)

    if "--idle-from-stdin" in sys.argv:
        try:
            data = json.loads(sys.stdin.read())
            session_id = data.get("session_id", "")
            label = resolve_session_label(session_id)
        except Exception:
            label = "unknown"
        import subprocess

        proc = subprocess.Popen(
            [sys.executable, __file__, "--idle", label],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path("/tmp/claude_watchdog.pid").write_text(str(proc.pid))
        sys.exit(0)

    if "--idle" in sys.argv:
        idx = sys.argv.index("--idle")
        session_label = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "unknown"
        time.sleep(300)
        ensure_bot_running()
        ipc(
            {
                "type": "notify",
                "text": f"**Claude is waiting for input** (5 min idle)\nSession: `{session_label}`",
                "session": session_label,
            }
        )
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
            parts = split_text(f"**Claude:**\n{last_text}")
            ipc_notify_parts(parts, session_label)
        sys.exit(0)

    if event == "SubagentStop":
        last_text = data.get("last_assistant_message", "")
        agent_type = data.get("agent_type", "subagent")
        if last_text:
            parts = split_text(f"**Claude [{agent_type}]:**\n{last_text}")
            ipc_notify_parts(parts, session_label)
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

    if tool == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        lines = ["**Claude Code: Questions**\n"]
        for q in questions:
            lines.append(
                f"**{q.get('header', q.get('question', '?'))}**: {q.get('question', '')}"
            )
            opts = q.get("options", [])
            for i, opt in enumerate(opts):
                lines.append(f"  {i + 1}. {opt.get('label', opt)}")
        lines.append(f"\nSession: `{session_label}`")
        msg_text = "\n".join(lines)
    elif tool == "ExitPlanMode":
        allowed = tool_input.get("allowedPrompts", [])
        header = "**Claude Code: Plan Approval Requested**\n"
        plans_dir = Path.home() / ".claude" / "plans"
        plan_files = sorted(
            plans_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True
        )
        plan_content = ""
        if plan_files:
            plan_content = plan_files[0].read_text().strip()
        footer_lines = []
        if allowed:
            footer_lines.append("Allowed prompts:")
            for p in allowed:
                footer_lines.append(f"  • [{p.get('tool', '?')}] {p.get('prompt', '')}")
        footer_lines.append(f"\nSession: `{session_label}`")
        footer = "\n".join(footer_lines)
        request_id = f"{session_label}:{int(time.time())}"
        if plan_content:
            plan_content = _wrap_plan_for_discord(plan_content)
            plan_chunks = split_text(
                plan_content, limit=1960
            )
            # Send overflow chunks as plain notify messages (already have their own fences)
            first_msg = f"{header}{plan_chunks[0]}"
            for chunk in plan_chunks[1:]:
                ipc({"type": "notify", "text": first_msg, "session": session_label})
                first_msg = chunk
            # Send the final chunk with buttons as the blocking approve message
            last = f"{first_msg}\n{footer}\nID: `{request_id}`"
        else:
            last = f"{header}{footer}\nID: `{request_id}`"
        plan_timeout = int(os.environ.get("DISCORD_PLAN_APPROVAL_TIMEOUT", "1800")) + 5
        result = ipc(
            {
                "type": "approve",
                "request_id": request_id,
                "text": last,
                "session": session_label,
                "permission_suggestions": [],
                "tool_name": tool,
                "tool_input": tool_input,
            },
            timeout=plan_timeout,
        )
        if result:
            decision = result["decision"]
            reason = result.get("reason", "")
            updated_input = result.get("updatedInput")
            if decision != "ask":
                updated_permissions = result.get("updatedPermissions")
                hook_output(decision, reason, event, updated_permissions, updated_input)
        return
    elif tool == "Bash":
        cmd = _sanitize_fences(tool_input.get("command", ""))
        bash_header = "**Claude Code: Approve?**\n\nTool: `Bash`\nCommand:\n"
        if len(cmd) > 1700:
            cmd_chunks = split_text(cmd, limit=1700)
            first_msg = f"{bash_header}```\n{cmd_chunks[0]}\n```"
            for chunk in cmd_chunks[1:]:
                ipc({"type": "notify", "text": first_msg, "session": session_label})
                first_msg = f"```\n{chunk}\n```"
            msg_text = first_msg + f"\nSession: `{session_label}`"
        else:
            msg_text = (
                f"{bash_header}```\n{cmd}\n```\n"
                f"Session: `{session_label}`"
            )
    else:
        yaml_input = to_yaml(tool_input)
        yaml_input = _sanitize_fences(yaml_input)
        if len(yaml_input) > 1800:
            yaml_input = yaml_input[:1800] + "\n..."
        msg_text = (
            f"**Claude Code: Approve?**\n\n"
            f"Tool: `{tool}`\nInput:\n```\n{yaml_input}\n```\n"
            f"Session: `{session_label}`"
        )

    # For AskUserQuestion, use blocking approve so Discord buttons can resolve it.
    if tool == "AskUserQuestion":
        request_id = f"{session_label}:{int(time.time())}"
        msg_text += f"\nID: `{request_id}`"
        result = ipc(
            {
                "type": "approve",
                "request_id": request_id,
                "text": msg_text,
                "session": session_label,
                "permission_suggestions": [],
                "tool_name": tool,
                "tool_input": tool_input,
            }
        )
        if result:
            decision = result["decision"]
            reason = result.get("reason", "")
            updated_input = result.get("updatedInput")
            if decision != "ask":
                hook_output(decision, reason, event, None, updated_input)
        return

    # Extract and display permission suggestion details for PermissionRequest events
    suggestions = (
        data.get("permission_suggestions", []) if event == "PermissionRequest" else []
    )
    if suggestions and tool != "AskUserQuestion":
        _dest_labels = {
            "localSettings": "local settings",
            "projectSettings": "project settings",
            "userSettings": "user settings",
            "session": "session only",
        }
        lines = ["\n**Permission Suggestions:**"]
        for i, s in enumerate(suggestions):
            stype = s.get("type", "")
            dest = _dest_labels.get(s.get("destination", ""), s.get("destination", ""))
            if stype == "addRules":
                behavior = s.get("behavior", "allow")
                rules = s.get("rules", [])
                for rule in rules:
                    tn = rule.get("toolName", "?")
                    rc = rule.get("ruleContent", "")
                    if rc:
                        lines.append(f"  • Add {behavior} rule: `{tn}` → `{rc}` ({dest})")
                    else:
                        lines.append(f"  • Add {behavior} rule: `{tn}` (any) ({dest})")
            elif stype == "setMode":
                mode = s.get("mode", "?")
                lines.append(f"  • Set mode: `{mode}` ({dest})")
            elif stype in ("replaceRules", "removeRules"):
                behavior = s.get("behavior", "?")
                lines.append(f"  • {stype} ({behavior}) ({dest})")
            elif stype in ("addDirectories", "removeDirectories"):
                dirs = s.get("directories", [])
                lines.append(f"  • {stype}: {', '.join(dirs)} ({dest})")
        msg_text += "\n" + "\n".join(lines)

    request_id = f"{session_label}:{int(time.time())}"
    msg_text += f"\nID: `{request_id}`"
    result = ipc(
        {
            "type": "approve",
            "request_id": request_id,
            "text": msg_text,
            "session": session_label,
            "permission_suggestions": suggestions,
            "tool_name": tool,
            "tool_input": tool_input,
        }
    )
    if result:
        decision = result["decision"]
        reason = result.get("reason", "")
        updated_input = result.get("updatedInput")
        if decision == "ask":
            # Bot unreachable or timed out — fall through to local prompt
            if event == "PermissionRequest":
                # No output = defer to default behavior
                pass
            else:
                hook_output("ask", reason, event)
        else:
            updated_permissions = result.get("updatedPermissions")
            hook_output(decision, reason, event, updated_permissions, updated_input)
    # No result at all: exit silently so Claude decides locally


if __name__ == "__main__":
    main()
