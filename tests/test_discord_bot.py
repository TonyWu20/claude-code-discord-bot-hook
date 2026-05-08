import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import discord
import pytest
import discord_bot


# ── thread cache ───────────────────────────────────────────────────────────────

def test_load_thread_ids_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, "THREAD_CACHE_FILE", tmp_path / "threads.json")
    assert discord_bot._load_thread_ids() == {}


def test_save_and_load_thread_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, "THREAD_CACHE_FILE", tmp_path / "threads.json")
    discord_bot._save_thread_id("sess1", 42)
    assert discord_bot._load_thread_ids() == {"sess1": 42}


# ── _add_notify_users ──────────────────────────────────────────────────────────

async def test_add_notify_users_calls_add_user(monkeypatch):
    thread = AsyncMock(spec=discord.Thread)
    thread.id = 999
    monkeypatch.setattr(discord_bot, "_NOTIFY_USER_IDS", [111, 222])
    await discord_bot._add_notify_users(thread)
    assert thread.add_user.call_count == 2
    calls = [c.args[0].id for c in thread.add_user.call_args_list]
    assert calls == [111, 222]


async def test_add_notify_users_skips_on_empty(monkeypatch):
    thread = AsyncMock(spec=discord.Thread)
    monkeypatch.setattr(discord_bot, "_NOTIFY_USER_IDS", [])
    await discord_bot._add_notify_users(thread)
    thread.add_user.assert_not_called()


async def test_add_notify_users_continues_on_http_error(monkeypatch, capsys):
    thread = AsyncMock(spec=discord.Thread)
    thread.id = 999
    thread.add_user.side_effect = discord.HTTPException(MagicMock(), "forbidden")
    monkeypatch.setattr(discord_bot, "_NOTIFY_USER_IDS", [111])
    await discord_bot._add_notify_users(thread)  # should not raise
    captured = capsys.readouterr()
    assert "[warn]" in captured.out


# ── handle_ipc_client ──────────────────────────────────────────────────────────

def _make_reader(payload: dict):
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline.return_value = (json.dumps(payload) + "\n").encode()
    return reader


def _make_writer():
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    written = []
    writer.write.side_effect = written.append
    writer._written = written
    return writer


async def test_handle_ipc_notify(decision_dir, monkeypatch):
    thread = AsyncMock()
    monkeypatch.setattr(discord_bot.bot, "get_channel", lambda _: MagicMock())
    with patch("discord_bot.get_or_create_session_thread", return_value=thread):
        reader = _make_reader({"type": "notify", "session": "s1", "text": "hello"})
        writer = _make_writer()
        await discord_bot.handle_ipc_client(reader, writer)

    thread.send.assert_called_once_with("hello")
    assert b'"ok": true' in writer._written[0]


async def test_handle_ipc_approve_decision(decision_dir, monkeypatch):
    thread = AsyncMock()
    monkeypatch.setattr(discord_bot.bot, "get_channel", lambda _: MagicMock())
    # Write decision file before the poll loop runs
    (decision_dir / "r1.json").write_text(json.dumps({"decision": "allow", "reason": "yes"}))

    with patch("discord_bot.get_or_create_session_thread", return_value=thread):
        reader = _make_reader({"type": "approve", "request_id": "r1", "session": "s1", "text": "ok?"})
        writer = _make_writer()
        await discord_bot.handle_ipc_client(reader, writer)

    response = json.loads(writer._written[0])
    assert response["decision"] == "allow"


async def test_handle_ipc_approve_timeout(decision_dir, monkeypatch):
    thread = AsyncMock()
    monkeypatch.setattr(discord_bot.bot, "get_channel", lambda _: MagicMock())
    monkeypatch.setattr(discord_bot, "APPROVAL_TIMEOUT", 0)

    with patch("discord_bot.get_or_create_session_thread", return_value=thread):
        reader = _make_reader({"type": "approve", "request_id": "r2", "session": "s1", "text": "ok?"})
        writer = _make_writer()
        await discord_bot.handle_ipc_client(reader, writer)

    response = json.loads(writer._written[0])
    assert response["decision"] == "ask"
    assert "Timed out" in response["reason"]


# ── on_interaction ─────────────────────────────────────────────────────────────

def _make_interaction(custom_id: str) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.type = discord.InteractionType.component
    interaction.data = {"custom_id": custom_id}
    interaction.response.send_message = AsyncMock()
    return interaction


async def test_on_interaction_approve(decision_dir):
    interaction = _make_interaction("approve:req-1")
    await discord_bot.on_interaction(interaction)
    data = json.loads((decision_dir / "req-1.json").read_text())
    assert data["decision"] == "allow"


async def test_on_interaction_deny(decision_dir):
    interaction = _make_interaction("deny:req-1")
    await discord_bot.on_interaction(interaction)
    data = json.loads((decision_dir / "req-1.json").read_text())
    assert data["decision"] == "deny"


async def test_on_interaction_approve_exit_plan(decision_dir, monkeypatch):
    tool_input = {"allowedPrompts": [{"tool": "Bash", "prompt": "run tests"}]}
    discord_bot._pending_tool_input["plan-req"] = tool_input
    interaction = _make_interaction("approve:plan-req")
    await discord_bot.on_interaction(interaction)
    data = json.loads((decision_dir / "plan-req.json").read_text())
    assert data["decision"] == "allow"
    assert data["updatedInput"] == tool_input
    assert "plan-req" not in discord_bot._pending_tool_input


async def test_on_interaction_askq_submit(decision_dir):
    questions = [
        {"question": "Which framework?", "header": "Framework",
         "options": [{"label": "React"}, {"label": "Vue"}], "multiSelect": False},
    ]
    discord_bot._pending_questions["q-req"] = {"questions": questions, "answers": {"Which framework?": "React"}}
    interaction = _make_interaction("askq_submit:q-req")
    await discord_bot.on_interaction(interaction)
    data = json.loads((decision_dir / "q-req.json").read_text())
    assert data["decision"] == "allow"
    assert data["updatedInput"]["answers"] == {"Which framework?": "React"}
    assert data["updatedInput"]["questions"] == questions
    assert "q-req" not in discord_bot._pending_questions


async def test_on_interaction_askq_select(decision_dir):
    questions = [
        {"question": "Which color?", "header": "Color",
         "options": [{"label": "Red"}, {"label": "Blue"}], "multiSelect": False},
    ]
    discord_bot._pending_questions["sel-req"] = {"questions": questions, "answers": {}}
    interaction = MagicMock(spec=discord.Interaction)
    interaction.type = discord.InteractionType.component
    interaction.data = {"custom_id": "askq:0:sel-req", "values": ["Red"]}
    interaction.response.send_message = AsyncMock()
    await discord_bot.on_interaction(interaction)
    assert discord_bot._pending_questions["sel-req"]["answers"] == {"Which color?": "Red"}
    discord_bot._pending_questions.pop("sel-req", None)


# ── helper tests ──────────────────────────────────────────────────────────────


def test_format_duration():
    assert discord_bot._format_duration(0) == "0s"
    assert discord_bot._format_duration(1000) == "1s"
    assert discord_bot._format_duration(30000) == "30s"
    assert discord_bot._format_duration(60000) == "1m"
    assert discord_bot._format_duration(90000) == "1m 30s"
    assert discord_bot._format_duration(3600000) == "1h"
    assert discord_bot._format_duration(3660000) == "1h 1m"
    assert discord_bot._format_duration(7200000) == "2h"


def test_format_number():
    assert discord_bot._format_number(0) == "0"
    assert discord_bot._format_number(100) == "100"
    assert discord_bot._format_number(10000) == "10,000"


def test_build_summary_text_empty():
    result = discord_bot._build_summary_text({"date": "2026-04-28", "projects": []})
    assert "No Claude Code activity" in result


def test_build_summary_text_with_data():
    summary = {
        "date": "2026-04-28",
        "projects": [
            {
                "name": "my-project",
                "duration_ms": 7200000,
                "total_tokens": 150000,
                "models": {
                    "model-a": {
                        "total": 100000, "input_tokens": 80000, "output_tokens": 20000,
                        "cache_read": 0, "cache_creation": 0,
                    },
                },
            },
        ],
    }
    result = discord_bot._build_summary_text(summary)
    assert "Claude Code Summary" in result
    assert "my-project" in result
    assert "150,000" in result
    assert "model-a" in result
    assert "80,000" in result


def test_summarize_usage_no_history(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, "HISTORY_FILE", tmp_path / "history.jsonl")
    # File doesn't exist
    result = discord_bot.summarize_usage("2026-04-28")
    assert result["date"] == "2026-04-28"
    assert result["projects"] == []


def test_summarize_usage_with_data(tmp_path, monkeypatch):
    # Use a timestamp that falls within 2026-04-28 UTC
    from datetime import timezone
    apr28 = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    ts_ms = int(apr28.timestamp() * 1000)

    # Set up history.jsonl
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps({"project": "/home/user/proj-a", "sessionId": "sess-1", "timestamp": ts_ms})
        + "\n"
    )
    monkeypatch.setattr(discord_bot, "HISTORY_FILE", history)

    # Set up project directory
    proj_dir = tmp_path / "projects" / "-home-user-proj-a"
    proj_dir.mkdir(parents=True)
    conv = proj_dir / "sess-1.jsonl"
    conv.write_text(
        json.dumps({
            "type": "user", "message": {"role": "user", "content": "hello"},
            "timestamp": "2026-04-28T10:00:00Z",
        })
        + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant", "content": [{"type": "text", "text": "hi"}],
                "model": "claude-opus-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "timestamp": "2026-04-28T10:00:05Z",
        })
        + "\n"
        + json.dumps({
            "type": "user", "message": {"role": "user", "content": "again"},
            "timestamp": "2026-04-28T10:01:00Z",
        })
        + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant", "content": [{"type": "text", "text": "ok"}],
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 30, "output_tokens": 20},
            },
            "timestamp": "2026-04-28T10:01:10Z",
        })
        + "\n"
    )
    monkeypatch.setattr(discord_bot, "PROJECTS_DIR", proj_dir.parent)

    result = discord_bot.summarize_usage("2026-04-28")
    assert result["date"] == "2026-04-28"
    assert len(result["projects"]) == 1
    proj = result["projects"][0]
    assert proj["name"] == "proj-a"
    assert proj["total_tokens"] == 200  # 100+50 + 30+20
    assert proj["duration_ms"] == 70000  # 10:01:10 - 10:00:00 = 70s
    assert set(proj["models"].keys()) == {"claude-opus-4-6", "claude-sonnet-4-6"}


def test_summarize_usage_skips_wrong_date(tmp_path, monkeypatch):
    """Sessions from a different date should be excluded."""
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps({"project": "/home/user/proj-a", "sessionId": "sess-2", "timestamp": 1700000000000})
        + "\n"
    )
    monkeypatch.setattr(discord_bot, "HISTORY_FILE", history)

    proj_dir = tmp_path / "projects" / "-home-user-proj-a"
    proj_dir.mkdir(parents=True)
    conv = proj_dir / "sess-2.jsonl"
    conv.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant", "content": [{"type": "text", "text": "hi"}],
                "model": "claude-opus-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            "timestamp": "2026-04-27T23:59:00Z",  # wrong date
        })
        + "\n"
    )
    monkeypatch.setattr(discord_bot, "PROJECTS_DIR", proj_dir.parent)

    result = discord_bot.summarize_usage("2026-04-28")
    assert result["projects"] == []


# ── sync state ─────────────────────────────────────────────────────────────────


def test_sync_state_load_save(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, "SYNC_STATE_FILE", tmp_path / "sync.json")
    state = {"sess-a": {"synced": True, "tmux_target": "main:0.1", "forum_thread_id": 123}}
    discord_bot._save_sync_state(state)
    assert discord_bot._load_sync_state() == state


def test_sync_state_load_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, "SYNC_STATE_FILE", tmp_path / "nonexistent.json")
    assert discord_bot._load_sync_state() == {}


# ── send_keys_to_tmux ──────────────────────────────────────────────────────────


def test_send_keys_to_tmux_success(monkeypatch):
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("discord_bot.subprocess.run", mock_run)
    assert discord_bot.send_keys_to_tmux("main:0.1", "hello") is True
    args = mock_run.call_args[0][0]
    assert args == ["tmux", "send-keys", "-t", "main:0.1", "hello", "Enter"]


def test_send_keys_to_tmux_multiline(monkeypatch):
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("discord_bot.subprocess.run", mock_run)
    assert discord_bot.send_keys_to_tmux("main:0.1", "line1\nline2") is True
    args = mock_run.call_args[0][0]
    assert args == ["tmux", "send-keys", "-t", "main:0.1", "line1", "C-j", "line2", "Enter"]


def test_send_keys_to_tmux_failure(monkeypatch):
    mock_run = MagicMock(return_value=MagicMock(returncode=1))
    monkeypatch.setattr("discord_bot.subprocess.run", mock_run)
    assert discord_bot.send_keys_to_tmux("main:0.1", "hello") is False


def test_send_keys_to_tmux_exception(monkeypatch):
    mock_run = MagicMock(side_effect=FileNotFoundError("tmux not found"))
    monkeypatch.setattr("discord_bot.subprocess.run", mock_run)
    assert discord_bot.send_keys_to_tmux("main:0.1", "hello") is False


# ── thread cache migration ─────────────────────────────────────────────────────


def test_thread_cache_keyed_by_session_id(tmp_path, monkeypatch):
    """After saving with a session_id key, load returns the same."""
    monkeypatch.setattr(discord_bot, "THREAD_CACHE_FILE", tmp_path / "threads.json")
    discord_bot._save_thread_id("sid-full-uuid", 42)
    assert discord_bot._load_thread_ids() == {"sid-full-uuid": 42}


def test_thread_cache_migration(tmp_path, monkeypatch):
    """Old label-keyed entries are auto-migrated to session_id keys on load."""
    cache = tmp_path / "threads.json"
    cache.write_text(json.dumps({"Tonys-Mac-mini-M4-my-session": 123}))
    monkeypatch.setattr(discord_bot, "THREAD_CACHE_FILE", cache)
    monkeypatch.setattr(discord_bot, "SESSIONS_DIR", tmp_path / "sessions")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    # Create a session file with matching label
    ss = sessions_dir / "99999.json"
    ss.write_text(json.dumps({
        "sessionId": "my-real-uuid-12345",
        "name": "my-session",
    }))
    ids = discord_bot._load_thread_ids()
    assert "my-real-uuid-12345" in ids
    assert ids["my-real-uuid-12345"] == 123
    # Cache file should have been rewritten with session_id key
    rewritten = json.loads(cache.read_text())
    assert "my-real-uuid-12345" in rewritten


# ── _resolve_session ───────────────────────────────────────────────────────────


def test_resolve_session_by_name(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    ss = sessions_dir / "111.json"
    ss.write_text(json.dumps({"sessionId": "abc-123", "name": "my-session", "cwd": "/project"}))
    monkeypatch.setattr(discord_bot, "SESSIONS_DIR", sessions_dir)
    result = discord_bot._resolve_session("my-session")
    assert result is not None
    assert result["sessionId"] == "abc-123"


def test_resolve_session_by_id_prefix(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    ss = sessions_dir / "222.json"
    ss.write_text(json.dumps({"sessionId": "abc-123", "name": "other-name", "cwd": "/proj"}))
    monkeypatch.setattr(discord_bot, "SESSIONS_DIR", sessions_dir)
    result = discord_bot._resolve_session("abc-")
    assert result is not None
    assert result["sessionId"] == "abc-123"


def test_resolve_session_not_found(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(discord_bot, "SESSIONS_DIR", sessions_dir)
    assert discord_bot._resolve_session("nonexistent") is None


# ── _resolve_sync_label ────────────────────────────────────────────────────────


def test_resolve_sync_label_with_name(monkeypatch):
    monkeypatch.setattr("discord_bot.socket.gethostname", lambda: "myhost.local")
    session = {"sessionId": "abc-123", "name": "dft-work"}
    assert discord_bot._resolve_sync_label(session) == "myhost-dft-work"


def test_resolve_sync_label_fallback_to_sid(monkeypatch):
    monkeypatch.setattr("discord_bot.socket.gethostname", lambda: "myhost.local")
    session = {"sessionId": "abc-123"}
    assert discord_bot._resolve_sync_label(session) == "myhost-abc-123"
