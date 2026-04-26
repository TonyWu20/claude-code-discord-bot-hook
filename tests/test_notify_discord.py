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
