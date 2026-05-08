import json
import os
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import notify_discord


# ── hook_output ────────────────────────────────────────────────────────────────

def test_hook_output_permission_allow(capsys):
    notify_discord.hook_output("allow", "ok", "PermissionRequest")
    out = json.loads(capsys.readouterr().out)
    hs = out["hookSpecificOutput"]
    assert hs["hookEventName"] == "PermissionRequest"
    assert hs["decision"] == {"behavior": "allow", "reason": "ok"}


def test_hook_output_permission_deny(capsys):
    notify_discord.hook_output("deny", "no", "PermissionRequest")
    out = json.loads(capsys.readouterr().out)
    hs = out["hookSpecificOutput"]
    assert hs["hookEventName"] == "PermissionRequest"
    assert hs["decision"] == {"behavior": "deny", "message": "no"}


def test_hook_output_pretooluse_legacy(capsys):
    notify_discord.hook_output("allow", "", "PreToolUse")
    out = json.loads(capsys.readouterr().out)
    hs = out["hookSpecificOutput"]
    assert hs["hookEventName"] == "PreToolUse"
    assert hs["permissionDecision"] == "allow"


def test_hook_output_with_updated_input_permission(capsys):
    ui = {"questions": [{"question": "Color?", "options": [{"label": "Red"}]}], "answers": {"Color?": "Red"}}
    notify_discord.hook_output("allow", "", "PermissionRequest", updated_input=ui)
    out = json.loads(capsys.readouterr().out)
    hs = out["hookSpecificOutput"]
    assert hs["hookEventName"] == "PermissionRequest"
    assert hs["decision"]["updatedInput"] == ui


def test_hook_output_with_updated_input_pretooluse(capsys):
    ui = {"allowedPrompts": []}
    notify_discord.hook_output("allow", "", "PreToolUse", updated_input=ui)
    out = json.loads(capsys.readouterr().out)
    hs = out["hookSpecificOutput"]
    assert hs["hookEventName"] == "PreToolUse"
    assert hs["updatedInput"] == ui


# ── ensure_bot_running ─────────────────────────────────────────────────────────

def test_ensure_bot_running_alive_and_ready(tmp_path, monkeypatch):
    pid_file = tmp_path / "bot.pid"
    ready_file = tmp_path / "bot.ready"
    pid_file.write_text(str(os.getpid()))
    ready_file.write_text("ready")

    monkeypatch.setattr(notify_discord, "PID_FILE", str(pid_file))
    monkeypatch.setattr(notify_discord, "READY_FILE", str(ready_file))

    with patch("notify_discord.subprocess.Popen") as mock_popen:
        notify_discord.ensure_bot_running()
    mock_popen.assert_not_called()


def test_ensure_bot_running_dead_pid(tmp_path, monkeypatch):
    pid_file = tmp_path / "bot.pid"
    ready_file = tmp_path / "bot.ready"
    pid_file.write_text("99999999")  # non-existent PID

    monkeypatch.setattr(notify_discord, "PID_FILE", str(pid_file))
    monkeypatch.setattr(notify_discord, "READY_FILE", str(ready_file))

    def fake_popen(*args, **kwargs):
        ready_file.write_text("ready")
        return MagicMock()

    with patch("notify_discord.subprocess.Popen", side_effect=fake_popen) as mock_popen:
        notify_discord.ensure_bot_running()

    assert not pid_file.exists()
    mock_popen.assert_called_once()


def test_ensure_bot_running_no_pid_file(tmp_path, monkeypatch):
    pid_file = tmp_path / "bot.pid"
    ready_file = tmp_path / "bot.ready"

    monkeypatch.setattr(notify_discord, "PID_FILE", str(pid_file))
    monkeypatch.setattr(notify_discord, "READY_FILE", str(ready_file))

    def fake_popen(*args, **kwargs):
        ready_file.write_text("ready")
        return MagicMock()

    with patch("notify_discord.subprocess.Popen", side_effect=fake_popen) as mock_popen:
        notify_discord.ensure_bot_running()

    mock_popen.assert_called_once()
    assert mock_popen.call_args[0][0][:2] == [notify_discord.VENV_PYTHON, notify_discord.BOT_SCRIPT]


# ── ipc ────────────────────────────────────────────────────────────────────────

def test_ipc_success():
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.recv.side_effect = [b'{"decision":"allow"}\n', b""]

    with patch("notify_discord.socket.socket", return_value=mock_sock):
        result = notify_discord.ipc({"type": "approve"})

    assert result == {"decision": "allow"}


def test_ipc_socket_error():
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.connect.side_effect = OSError("refused")

    with patch("notify_discord.socket.socket", return_value=mock_sock):
        result = notify_discord.ipc({"type": "approve"})

    assert result is None


# ── _extract_fence_lang ──────────────────────────────────────────────────


def test_extract_fence_lang_bare():
    assert notify_discord._extract_fence_lang("```\ncode") == "```\n"


def test_extract_fence_lang_python():
    assert notify_discord._extract_fence_lang("```python\ncode") == "```python\n"


def test_extract_fence_lang_no_newline():
    assert notify_discord._extract_fence_lang("```") == "```\n"


# ── split_text long code blocks ──────────────────────────────────────────


def test_split_text_long_code_block_balanced():
    """When a single code block exceeds the limit, every chunk must have balanced fences."""
    lines = [f"    result_{i} = compute(input_data)" for i in range(300)]
    code_block = "```python\n" + "\n".join(lines) + "\n```"
    text = "Here is code:\n\n" + code_block + "\n\nDone."
    parts = notify_discord.split_text(text, limit=1990)
    for i, part in enumerate(parts):
        assert part.count("```") % 2 == 0, f"Part {i} has unbalanced fences: {part!r}"


def test_split_text_preserves_fence_lang():
    """Long code block split across chunks reopens with the language specifier."""
    lines = [f"print({i})" for i in range(400)]
    code_block = "```python\n" + "\n".join(lines) + "\n```"
    text = "Intro\n" + code_block + "\nOutro"
    parts = notify_discord.split_text(text, limit=1990)
    # At least one chunk after the first should reopen with ```python
    reopened = any(
        p.lstrip().startswith("```python") for p in parts[1:]
    )
    assert reopened, "No chunk after the first has ```python"


def test_split_text_normal_intact():
    """Short code block within limit stays as one chunk unchanged."""
    text = "Before\n```python\nx = 1\n```\nAfter"
    parts = notify_discord.split_text(text, limit=1990)
    assert len(parts) == 1
    assert parts[0] == text


def test_split_text_no_fences_no_regression():
    """Plain text (no fences) splits correctly on newlines."""
    lines = [f"line {i}" for i in range(100)]
    text = "\n".join(lines)
    parts = notify_discord.split_text(text, limit=500)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 500


def test_split_text_underline_inside_fence():
    """x86_64-linux inside a long code block must stay inside fences after
    splitting — otherwise Discord renders _64_ as italics."""
    # Build a long code block with x86_64-linux near the split boundary
    lines = [f"item {i}" for i in range(200)]
    # Position x86_64-linux where the split is likely to land
    lines.insert(100, "platform = x86_64-linux")
    code_block = "```\n" + "\n".join(lines) + "\n```"
    text = "Before\n\n" + code_block + "\n\nAfter"
    parts = notify_discord.split_text(text, limit=1000)
    assert len(parts) > 1, "Should split into multiple chunks"
    for i, part in enumerate(parts):
        assert part.count("```") % 2 == 0, \
            f"Part {i} has unbalanced fences: {part!r}"
        # x86_64-linux must be inside a code block if present
        if "x86_64-linux" in part:
            idx = part.index("x86_64-linux")
            prefix = part[:idx]
            assert notify_discord._in_fence(prefix), \
                f"x86_64-linux outside fence in part {i}"


# ── IPC enrichment tests ──────────────────────────────────────────────────────


def test_ipc_notify_parts_includes_session_id(monkeypatch):
    """Notify messages should include session_id when provided."""
    calls = []
    def fake_ipc(req, timeout=None):
        calls.append(req)
        return None
    monkeypatch.setattr(notify_discord, "ipc", fake_ipc)
    notify_discord.ipc_notify_parts(["msg1"], "sess-label", "abc-123")
    assert calls[0].get("session_id") == "abc-123"
    assert calls[0].get("session") == "sess-label"
    assert calls[0].get("type") == "notify"


def test_ipc_notify_parts_includes_tmux_target(monkeypatch):
    """Notify messages should include tmux_target when provided."""
    calls = []
    def fake_ipc(req, timeout=None):
        calls.append(req)
        return None
    monkeypatch.setattr(notify_discord, "ipc", fake_ipc)
    notify_discord.ipc_notify_parts(["msg1"], "sess-label", tmux_target="main:0.1")
    assert calls[0].get("tmux_target") == "main:0.1"


def test_ipc_notify_parts_no_extra_when_empty(monkeypatch):
    """Notify messages should not include empty session_id or tmux_target."""
    calls = []
    def fake_ipc(req, timeout=None):
        calls.append(req)
        return None
    monkeypatch.setattr(notify_discord, "ipc", fake_ipc)
    notify_discord.ipc_notify_parts(["msg1"], "sess-label")
    assert "session_id" not in calls[0]
    assert "tmux_target" not in calls[0]
