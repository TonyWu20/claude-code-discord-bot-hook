# Change Log

## [0.4.0] 2026-04-22 — AskUserQuestion and ExitPlanMode Discord support

**Changed:** `hooks/notify_discord.py`, `hooks/discord_bot.py`
**Why:** `AskUserQuestion` and `ExitPlanMode` were intercepted by the hook but had no Discord UI — decisions were never resolved, causing Claude Code to always time out before showing its local dialog.
**What:**
- Fix `request_id` truncation bug in all button/select interaction handlers (`approve`, `deny`, `askq_submit`) — the `request_id` contains a colon (`session:timestamp`) which `split(":", 2)` was splitting incorrectly; fixed by stripping the action prefix directly
- `AskUserQuestion`: render Select menus per question with a **Submit Answers** button; add **Answer with text** button that opens a Discord Modal with a `TextInput` per question for free-text responses
- `ExitPlanMode`: include the plan file content (most recently modified `.md` in `~/.claude/plans/`) in the Discord message; add a **Give Feedback** button that opens a Modal so you can type revision instructions, which are returned as a deny with your feedback as the reason

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
