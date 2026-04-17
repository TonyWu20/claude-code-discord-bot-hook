import os
import pytest

# Set env vars before discord_bot is imported (module-level os.environ[] access)
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("DISCORD_CHANNEL_ID", "123456789")
os.environ.setdefault("DISCORD_INSPECT_CHANNEL_ID", "123456789")


@pytest.fixture
def decision_dir(tmp_path, monkeypatch):
    import discord_bot
    d = tmp_path / "decisions"
    d.mkdir()
    monkeypatch.setattr(discord_bot, "DECISION_DIR", d)
    return d
