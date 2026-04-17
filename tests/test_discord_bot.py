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
