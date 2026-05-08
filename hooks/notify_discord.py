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

DISCORD_BOT_HOST = os.environ.get("DISCORD_BOT_HOST", "")
DISCORD_BOT_REMOTE = os.environ.get("DISCORD_BOT_REMOTE", "")
TMUX_CACHE_FILE = Path("/tmp/claude_discord_tmux.json")


def ensure_bot_running() -> None:
    if DISCORD_BOT_HOST and DISCORD_BOT_REMOTE:
        # Remote mode — bot lifecycle managed on the server machine
        return
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
        if DISCORD_BOT_HOST:
            host, port = DISCORD_BOT_HOST.split(":", 1)
            sock_family = socket.AF_INET
            sock_addr = (host, int(port))
        else:
            sock_family = socket.AF_UNIX
            sock_addr = SOCKET_PATH
        with socket.socket(sock_family, socket.SOCK_STREAM) as s:
            if timeout is None:
                timeout = int(os.environ.get("DISCORD_APPROVAL_TIMEOUT", "120")) + 5
            s.settimeout(timeout)
            s.connect(sock_addr)
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


def ipc_notify_parts(
    parts: list[str], session: str, session_id: str = "", tmux_target: str | None = None,
) -> None:
    """Send multiple text parts as sequential notify messages."""
    for part in parts:
        payload: dict[str, object] = {"type": "notify", "text": part, "session": session}
        if session_id:
            payload["session_id"] = session_id
        if tmux_target:
            payload["tmux_target"] = tmux_target
        ipc(payload)


def hook_output(
    decision: str,
    reason: str = "",
    updated_permissions: Optional[list] = None,
    updated_input: Optional[dict] = None,
) -> None:
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
    hook_specific_output: dict = {
        "hookEventName": "PermissionRequest",
        "decision": d,
    }
    if updated_input is not None:
        hook_specific_output["updatedInput"] = updated_input
    print(json.dumps({"hookSpecificOutput": hook_specific_output}))


def resolve_session_label(session_id: str) -> str:
    host = socket.gethostname().split(".")[0]
    sessions_dir = Path.home() / ".claude" / "sessions"
    try:
        pid_file = sessions_dir / f"{os.getppid()}.json"
        if pid_file.exists():
            data = json.loads(pid_file.read_text())
            return f"{host}-{data.get('name') or session_id[:8]}"
        for f in sessions_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("sessionId") == session_id:
                    return f"{host}-{data.get('name') or session_id[:8]}"
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return f"{host}-{session_id[:8]}"


def discover_tmux_target(claude_pid: int) -> Optional[str]:
    """Find the tmux pane target (e.g. '0:1.1') for a given PID.

    Walks the process ancestor tree via `ps` and matches against all
    tmux pane PIDs. PID-based, not focus-based — correct even when the
    target pane is not focused.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        panes: list[tuple[int, str]] = []
        for line in result.stdout.strip().split("\n"):
            if " " in line:
                ppid_str, target = line.split(" ", 1)
                try:
                    panes.append((int(ppid_str), target.strip()))
                except ValueError:
                    continue
        # Walk ancestor tree of claude_pid using `ps` (no psutil needed)
        ancestors = {claude_pid}
        current = claude_pid
        visited = {claude_pid}
        for _ in range(50):
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
        return None


def _cache_tmux_target(session_label: str, target: str | None) -> None:
    try:
        cache = json.loads(TMUX_CACHE_FILE.read_text()) if TMUX_CACHE_FILE.exists() else {}
        if target:
            cache[session_label] = target
        else:
            cache.pop(session_label, None)
        TMUX_CACHE_FILE.write_text(json.dumps(cache))
    except OSError:
        pass


def _in_fence(text: str) -> bool:
    """Return True if ``text`` is inside an open ``` fenced code block."""
    count = 0
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            count += 1
    return count % 2 == 1


def split_text(text: str, limit: int = 1990) -> list[str]:
    """Split text into chunks <= limit chars, never breaking inside ``` fences."""
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
        # Don't split inside a fenced code block
        if cut > 0 and _in_fence(text[:cut]):
            prev = text.rfind("\n```", 0, cut)
            if prev > 0:
                # Inside a code block — close fence at cut, reopen in next chunk
                fence_header = _extract_fence_lang(text[prev:].lstrip("\n"))
                parts.append(text[:cut] + "\n```")
                text = fence_header + text[cut:].lstrip("\n")
                continue
            elif text.startswith("```"):
                # Fence starts at beginning — find the closing ``` to include
                closing = text.find("\n```\n", 4)
                if closing > 0 and closing + 5 <= limit + 200:
                    cut = closing + 5
                else:
                    # Code block too long — close fence, reopen in next chunk
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


def _extract_fence_lang(text: str) -> str:
    """Return the opening fence line (e.g. '```python\n') from text starting with ```."""
    newline = text.find("\n")
    if newline == -1:
        return "```\n"
    return text[: newline + 1]


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
            session_id = ""
        import subprocess

        proc = subprocess.Popen(
            [sys.executable, __file__, "--idle", label, "--session-id", session_id],
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
        session_id = ""
        if "--session-id" in sys.argv:
            sidx = sys.argv.index("--session-id")
            session_id = sys.argv[sidx + 1] if len(sys.argv) > sidx + 1 else ""
        time.sleep(300)
        ensure_bot_running()
        payload: dict[str, object] = {
            "type": "notify",
            "text": f"**Claude is waiting for input** (5 min idle)\nSession: `{session_label}`",
            "session": session_label,
        }
        if session_id:
            payload["session_id"] = session_id
        ipc(payload)
        sys.exit(0)

    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    event = data.get("hook_event_name", "")
    session_id = data.get("session_id", "")
    session_label = resolve_session_label(session_id)

    # Discover tmux target and cache it
    tmux_target = discover_tmux_target(os.getppid())
    _cache_tmux_target(session_label, tmux_target)

    ensure_bot_running()

    if event == "Stop":
        last_text = data.get("last_assistant_message", "")
        if last_text:
            parts = split_text(f"**Claude:**\n{last_text}")
            ipc_notify_parts(parts, session_label, session_id, tmux_target)
        sys.exit(0)

    if event == "SubagentStop":
        last_text = data.get("last_assistant_message", "")
        agent_type = data.get("agent_type", "subagent")
        if last_text:
            parts = split_text(f"**Claude [{agent_type}]:**\n{last_text}")
            ipc_notify_parts(parts, session_label, session_id, tmux_target)
        sys.exit(0)

    if event != "PermissionRequest":
        sys.exit(0)

    # Check for stop flag before processing tool approval
    flag_path = Path(f"/tmp/claude_stop_{session_label}.txt")
    if flag_path.exists():
        reason = flag_path.read_text().strip() or "Stopped via Discord"
        flag_path.unlink(missing_ok=True)
        hook_output("deny", reason)
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
            plan_chunks = split_text(
                plan_content, limit=1960
            )
            # Send overflow chunks as plain notify messages
            first_msg = f"{header}{plan_chunks[0]}"
            for chunk in plan_chunks[1:]:
                ipc({"type": "notify", "text": first_msg, "session": session_label, "session_id": session_id, "tmux_target": tmux_target or ""})
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
                "session_id": session_id,
                "tmux_target": tmux_target or "",
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
                hook_output(decision, reason, updated_permissions=updated_permissions, updated_input=updated_input)
        return
    elif tool == "Bash":
        cmd = tool_input.get("command", "")
        bash_header = "**Claude Code: Approve?**\n\nTool: `Bash`\nCommand:\n"
        if len(cmd) > 1700:
            cmd_chunks = split_text(cmd, limit=1700)
            first_msg = f"{bash_header}```\n{cmd_chunks[0].replace('```', '｀｀｀')}\n```"
            for chunk in cmd_chunks[1:]:
                ipc({"type": "notify", "text": first_msg, "session": session_label, "session_id": session_id, "tmux_target": tmux_target or ""})
                first_msg = f"```\n{chunk.replace('```', '｀｀｀')}\n```"
            msg_text = first_msg + f"\nSession: `{session_label}`"
        else:
            msg_text = (
                f"{bash_header}```\n{cmd.replace('```', '｀｀｀')}\n```\n"
                f"Session: `{session_label}`"
            )
    else:
        yaml_input = to_yaml(tool_input)
        msg_text = (
            f"**Claude Code: Approve?**\n\n"
            f"Tool: `{tool}`\nInput:\n```\n{yaml_input.replace('```', '｀｀｀')}\n```\n"
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
                "session_id": session_id,
                "tmux_target": tmux_target or "",
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
                hook_output(decision, reason, updated_input=updated_input)
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
            "session_id": session_id,
            "tmux_target": tmux_target or "",
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
                hook_output("ask", reason)
        else:
            updated_permissions = result.get("updatedPermissions")
            hook_output(decision, reason, updated_permissions=updated_permissions, updated_input=updated_input)
    # No result at all: exit silently so Claude decides locally


if __name__ == "__main__":
    main()
