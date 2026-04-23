import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
