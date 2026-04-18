# Change Log

## [0.3.0] 2026-04-18 — Permission suggestion buttons

**Changed:** `hooks/notify_discord.py`, `hooks/discord_bot.py`
**Why:** The hook's bare `allow`/`deny` response could not close Claude Code's permission dialog when `permission_suggestions` were present; the dialog expected an `updatedPermissions` entry
**What:**
- Pass `permission_suggestions` from hook input through IPC to the Discord bot
- Render suggestion-specific buttons (e.g. "Allow + allow rule (local)") alongside Approve/Deny
- Wrap `PermissionRequest` output in `hookSpecificOutput` envelope to match the expected format
- Selected suggestion is echoed back as `updatedPermissions`, properly closing the dialog

## [2026-04-17] Add pytest test suite

**Changed:** `hooks/pyproject.toml`, new `tests/` directory (conftest.py, test_notify_discord.py, test_discord_bot.py)
**Why:** Recurring regressions in established features after new additions; zero test coverage existed
**Risk:** None — additive only
