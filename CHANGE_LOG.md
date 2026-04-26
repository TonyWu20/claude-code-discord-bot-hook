# Change Log

## [0.5.0] 2026-04-27 — Suggestion details, Edit Rule, code block nesting fix, test harness

**Changed:** `hooks/discord_bot.py`, `hooks/notify_discord.py`, `tests/test_notify_discord.py`
**New:** `hooks/tests/fixtures/*.json` (9 files), `hooks/tests/simulate.py`
**Why:** Users couldn't see what permission suggestion they were approving (button labels
  were too short); had no way to edit rules before applying; long Bash commands were
  silently truncated; plan content with embedded code blocks broke Discord formatting;
  no way to test hooks without a real Claude Code session.
**What:**
- **Suggestion detail text:** Permission suggestion content (tool name, rule pattern,
  destination) is now displayed inline in the Discord message, not hidden behind
  button labels
- **Edit Rule button:** Each `addRules` suggestion gets an Edit Rule button that opens
  a Discord Modal pre-filled with the current `ruleContent`; edits are written back as
  the approval decision with the modified suggestion
- **Bash command pagination:** Long Bash commands (>1700 chars) are split across
  multiple Discord messages instead of being silently truncated at 1800 chars —
  overflow chunks are sent as notify messages; the last chunk has the interactive
  buttons
- **Code block nesting fix (plans):** `_wrap_plan_for_discord()` splits plan content at
  its own ` ``` ` fence boundaries into alternating ```` ```markdown ```` / ```` ```lang ````
  blocks so code blocks inside a plan render correctly without breaking the outer
  formatting
- **Code block nesting fix (all tool input):** `_sanitize_fences()` replaces triple
  backticks with fullwidth grave accents in Bash commands, generic tool YAML, history
  views, and tool results — prevents any content containing ` ``` ` from breaking
  Discord code block fencing
- **Test fixtures:** 9 JSON fixture files covering all supported hook event types
  (PermissionRequest, PreToolUse, Notification, Stop, SubagentStop, UserPromptSubmit)
- **Test harness:** `tests/simulate.py` with `--dry-run` (validate parsing without bot)
  and normal mode (pipe fixtures through the real hook pipeline)
- **Bug fix:** Corrected `test_hook_output_permission_deny` assertion from `reason` to
  `message` — the hook spec uses `message` for deny decisions

## [0.4.2] 2026-04-27 — Increase plan approval timeout default to 1800 s

**Changed:** `hooks/discord_bot.py`, `hooks/notify_discord.py`
**Why:** The 900 s (15 min) default was too tight for reviewing large plans; reviewers need more time to read and give feedback.
**What:**
- Bump `DISCORD_PLAN_APPROVAL_TIMEOUT` default from `"900"` to `"1800"` (30 min) in both the bot poll loop and the socket timeout

## [0.4.1] 2026-04-27 — Fix ExitPlanMode timeout for plan review feedback

**Changed:** `hooks/discord_bot.py`, `hooks/notify_discord.py`
**Why:** The 120 s approval timeout was too short for ExitPlanMode — reading an implementation plan takes minutes, and feedback submitted after the poll loop expired was silently lost.
**What:**
- Add `DISCORD_PLAN_APPROVAL_TIMEOUT` env var (default `"900"` / 15 min) so ExitPlanMode gets its own longer poll deadline
- `discord_bot.py`: use `PLAN_APPROVAL_TIMEOUT` in the decision poll loop when `tool_name == "ExitPlanMode"`
- `notify_discord.py`: add `timeout` parameter to `ipc()` and pass `DISCORD_PLAN_APPROVAL_TIMEOUT + 5` for ExitPlanMode socket timeout
- All other blocking hooks (Bash, AskUserQuestion, etc.) continue to use the original `DISCORD_APPROVAL_TIMEOUT` (120 s)

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
