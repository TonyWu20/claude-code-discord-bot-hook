# Change Log

## [2.0.0] 2026-05-09 — Bidirectional sync milestone

**Changed:** `hooks/pyproject.toml`, `CHANGE_LOG.md`, `ARCHITECTURE.md`
**Marketplace:** `my-claude-marketplace` `v2.0.0`

**Why:** The bidirectional session sync feature (`/sync` command, tmux pane discovery,
forum-based conversation mirroring, prompt forwarding) landed in 0.10.0 and has been
stable through 0.10.1 and 0.11.0 refinements. This release marks the major feature
milestone by aligning the project version with the marketplace version.

**What:**
- Bump project version to `2.0.0` across all files to match the marketplace manifest.

## [0.11.0] 2026-05-09 — Conversation context, zero truncation, PreToolUse cleanup

**Changed:** `hooks/discord_bot.py`, `hooks/notify_discord.py`, `tests/test_discord_bot.py`,
`tests/test_notify_discord.py`, `ARCHITECTURE.md`, `README.md`
**Added:** `tests/__init__.py`

**Why:** Three issues:
1. PermissionRequest showed only tool details — no conversation context leading up to the request.
2. `_sanitize_fences()` replaced ``` with ugly fullwidth ｀｀｀, losing syntax highlighting.
3. `PreToolUse` dead code in notify_discord.py blocked Claude Code's dialog prompt when triggered.

**What:**

### Conversation context
- Implemented `_find_last_user_message_idx()` and `_get_conversation_context()`.
- Context (messages from last user prompt onward: text, thinking, tool_use, tool_result) sent as separate messages before the approval message in both the session thread and the synced forum thread.
- If context exceeds Discord's message limit, it's split via `split_text()` across multiple messages — nothing is truncated.

### Inner code blocks
- All code-block-wrapped content (tool_use JSON, tool_results, Bash commands, YAML input) now sanitizes inner ``` to fullwidth ｀｀｀ via inline `.replace('```', '｀｀｀')`. This is the only approach that reliably prevents inner ``` from breaking outer code block fencing on Discord, especially for JSON blocks where ``` appears inside string values.
- `_sanitize_fences()` function removed. `_safe_code_block()` retained as an internal helper for `split_text()` but not used for display blocks.

### No truncation anywhere
- Removed all per-block truncations from `extract_messages()` (was 1500 chars on tool_use, tool_result, thinking).
- Removed per-message truncation from `_get_conversation_context()` (was 500 chars).
- Removed truncation from `format_message()` (was 1800 chars).
- Removed truncation from `_activate_sync()` first content chunk (was 1900 chars).
- Removed truncation from forum text forwarding in `handle_ipc_client` (was 1900 chars).
- Removed YAML input truncation in `notify_discord.py` (was 1800 chars).
- All size management handled by `split_text()` — content is split across multiple messages, never cut.

### PreToolUse dead code removed
- `hook_output()` no longer accepts an `event` parameter — only `PermissionRequest` format.
- Event routing simplified: `if event not in ("PreToolUse", "PermissionRequest")` → `if event != "PermissionRequest"`.
- Updated README: PreToolUse removed from Supported Hook Events table; Notification row removed (never implemented).

### `split_text()` fence boundary fix
- When `split_text()` splits inside a code block, it now properly closes the fence at the cut point and reopens it in the next chunk — no orphaned fences or leaked content at chunk boundaries.
- `split_text()` added to `discord_bot.py` (was only in `notify_discord.py`).

### Test infrastructure
- `tests/__init__.py` added, `pytest.ini` gains `pythonpath = hooks`.
- 69 tests pass (was 56 passing + 12 failing).

## [0.10.2] — Fix multi-question AskUserQuestion answer submission via PermissionRequest

**Changed:** `hooks/notify_discord.py`, `tests/test_discord_bot.py`, `tests/test_notify_discord.py`

**Why:** When Claude Code asked multiple questions via `AskUserQuestion`, the answers submitted in Discord were not received by Claude Code when routed through `PermissionRequest`. The `updatedInput` was nested inside `decision` in the `hookSpecificOutput`, but Claude Code's AskUserQuestion processing expects it at the **top level** of `hookSpecificOutput`. Single questions happened to work (likely because Claude Code handles them inline), but multi-question consistently failed.

**What:**
- `hook_output()` now places `updatedInput` at BOTH the top level of `hookSpecificOutput` (for AskUserQuestion processing) and inside `decision` (for backward compatibility with other PermissionRequest handlers).
- Added multi-question tests: submit with 2 questions (single-select + multi-select), select accumulation across 2 questions, and colon-in-`request_id` patterns.

## [0.10.1] 2026-05-08 — Reliable message forwarding, submission confirmation, and single-instance guard

**Changed:** `hooks/discord_bot.py`, `hooks/notify_discord.py`, `ARCHITECTURE.md`, `README.md`, `tests/test_discord_bot.py`, `tests/test_notify_discord.py`
**Removed:** `psutil` from `hooks/pyproject.toml`

**Why:** The session sync feature in 0.10.0 had several reliability issues. tmux pane discovery silently failed (psutil import error). The Enter key for submission was unreliable from subprocess contexts. Multiple bot instances could run simultaneously. Tool results were truncated at 500 chars and thinking content was invisible.

**What:**

### psutil → `ps` command migration
- Both `discover_tmux_target_for_session()` and `discover_tmux_target()` now walk the process tree via `subprocess.run(["ps", "-o", "ppid=", ...])` instead of `import psutil`. Eliminates silent import failures in marketplace plugin installs.
- `psutil` removed from `hooks/pyproject.toml`.

### IPC handler: always update tmux_target
- `handle_ipc_client` now updates `tmux_target` in `_session_sync` regardless of whether the session is already registered. Previously the guard `if session not in _session_sync` left the target empty for sessions activated via `/sync`.

### on_message: on-demand tmux discovery
- When the cached `tmux_target` is empty, `on_message` calls `discover_tmux_target_for_session()` as a fallback before giving up with ⚠️.

### Reliable message submission via `subprocess.run`
- `send_keys_to_tmux` uses `subprocess.run` with a list of arguments (no shell). Newlines in messages are sent as `C-j` (Ctrl+J) which inserts literal line breaks in the TUI input widget. A final `"Enter"` submits the complete multiline text as one prompt.

### Submission confirmation with JSONL polling
- After sending to tmux, `_confirm_message_submitted()` polls the session's JSONL conversation file for growth. ✅ is added to the Discord message only when Claude has processed it. If the file doesn't grow within 3 seconds, Enter is retried up to 4 more times at 2s intervals. Falls back to ⚠️ if all retries fail.

### Instance lock via fcntl.flock
- OS-level atomic file lock prevents multiple bot instances from running simultaneously. Lock is acquired before Discord connection and released on exit.

### Thinking content display
- Assistant messages with `{"type": "thinking"}` blocks are displayed in forum threads with a 💭 prefix.

### Extended tool result display
- Tool result and input truncation raised from 500 to 1500 characters.

### Permission request forwarding
- Approval requests are forwarded to the synced forum thread as text notifications (without buttons — approval still happens in the session thread).

### Default approval timeout
- `DISCORD_APPROVAL_TIMEOUT` default raised from 120s to 300s (5 minutes).

### Tests
- 54 tests (1 new: multiline send_keys_to_tmux). All pass.

## [0.10.0] 2026-05-08 — Bidirectional Discord control with session sync

**Changed:** `hooks/discord_bot.py`, `hooks/notify_discord.py`, `hooks/pyproject.toml`, `ARCHITECTURE.md`, `tests/test_discord_bot.py`, `tests/test_notify_discord.py`
**New:** `pytest.ini`

**Why:** The bot was a read-only monitor — you could see what Claude was doing and approve/deny actions, but you couldn't see full conversation output or send prompts back into a session from Discord. This made it impossible to continue a session while away from the computer without SSH + tmux on phone.

**What:**

### Session rename regression fix (thread orphan)
- Thread cache (`/tmp/claude_discord_threads.json`) now keyed by immutable `session_id` instead of mutable session label
- `get_or_create_session_thread()` takes `(session_id, session_label)` — looks up by session_id, names by label
- When a session is renamed mid-session, the bot detects the mismatch and calls `thread.edit(name=...)` in-place
- Old label-keyed cache entries are auto-migrated to session_id keys on bot startup

### `/sync` slash command
- `/sync` with no args: Select menu of active Claude Code sessions → pick one → starts sync
- `/sync off`: Select menu of currently-synced sessions → pick one → stops sync
- `/sync <name>`: Direct ON by session name, sessionId prefix, or hostname-prefixed label
- `/sync <name> off`: Direct OFF

### Session sync to forum posts
- `/sync on` creates a forum post in `DISCORD_SYNC_CHANNEL_ID` and dumps full conversation history
- On each Stop event: new messages (from session JSONL) are posted as replies in the forum thread
- On session end: "Session ended. Sync disabled." posted to forum, sync auto-disabled
- Sync state persisted to `/tmp/claude_discord_sync.json`

### tmux pane discovery and prompt injection
- **PID-based discovery** (not focus-based): `discover_tmux_target()` walks the process ancestor tree via psutil and matches against `tmux list-panes -a -F '#{pane_pid}'` — correct even when the Claude Code pane is not focused
- Shim-side discovery: `notify_discord.py` calls `discover_tmux_target(os.getppid())` on every hook invocation, passes `tmux_target` in all IPC messages
- Bot-side discovery: `discord_bot.py` has `discover_tmux_target_for_session()` for the fallback case (user types `/sync on` before any hook event fires)
- `send_keys_to_tmux()`: runs `tmux send-keys -t <target> <text> Enter`; reacts ✅ on success, ⚠️ or error reply on failure

### Message forwarding from forum → tmux
- `on_message` handler detects user messages in synced forum threads
- Forwards them to the running Claude Code tmux pane via `tmux send-keys`
- Bot's own messages and slash commands are skipped

### IPC enrichment
- All IPC messages from `notify_discord.py` now carry `session_id` (stable key) and `tmux_target` (for pane targeting)
- Both are optional — backward compatible with unmodified bots

### Dependency
- Added `psutil>=5.9` for cross-platform process tree walking (needed for tmux pane discovery on macOS, which has no `/proc`)

### Tests
- 15 new tests covering: sync state load/save, send_keys_to_tmux success/failure, thread cache keyed by session_id, cache migration from old format, session resolution by name/id, IPC enrichment (session_id, tmux_target in messages)

## [0.9.0] 2026-05-08 — Hostname prefix in session labels and request IDs

**Changed:** `hooks/notify_discord.py`, `ARCHITECTURE.md`, `README.md`
**Why:** When using the hook across multiple machines, session labels and request IDs had no indication of which machine they came from, making multi-machine setups confusing to read.
**What:**
- Prepend the short hostname (e.g., `tony-mbp-`) to the session label produced by `resolve_session_label()`. The hostname prefix automatically propagates to request IDs, Discord thread names, stop flags, decision files, and IPC routing keys — everything that derives from the session label.
- Updated docs to reflect the new label format.

## [0.8.1] 2026-05-08 — Fix plugin hook paths for marketplace portability

**Changed:** `hooks/hooks.json`, `README.md`
**Why:** The plugin's `hooks.json` hardcoded `~/.claude/hooks/.venv/bin/python` paths, which only worked via a local symlink on the author's machine. The `.venv` directory is not shipped with the marketplace plugin, so hook commands failed silently on fresh installs.
**What:**
- Replaced all `.venv/bin/python` invocations with `uv run --directory ${CLAUDE_PLUGIN_ROOT}/hooks python`, which auto-creates the venv from `pyproject.toml` on first run.
- README: moved `uv` installation to a prerequisite step (it's required, not optional). Removed the manual `uv sync` step — `uv run` handles this automatically.

## [0.8.0] 2026-05-02 — Multi-machine TCP IPC support

**Changed:** `hooks/discord_bot.py`, `hooks/notify_discord.py`, `ARCHITECTURE.md`
**Why:** Users wanted to run the Discord bot on a different machine than Claude Code — e.g., a server in the same ZeroTier/Tailscale/LAN network — so approval flows work remotely without installing the bot on every client.
**What:**
- **`discord_bot.py`:** `run_socket_server()` now branches on `DISCORD_BOT_HOST` — when set, binds a TCP server (host:port) instead of a Unix socket. PID file and ready file handling is identical in both modes.
- **`notify_discord.py`:** `ipc()` connects via `AF_INET` when `DISCORD_BOT_HOST` is set, `AF_UNIX` otherwise. Same JSON-line protocol over both transports.
- **`notify_discord.py`:** `ensure_bot_running()` skips local bot spawn when `DISCORD_BOT_REMOTE=true` — the remote machine manages its own bot lifecycle.
- **New env vars:** `DISCORD_BOT_HOST` (TCP host:port, e.g. `0.0.0.0:9876`) and `DISCORD_BOT_REMOTE` (skip local spawn).
- **Backward-compatible:** When both are unset (default), behavior is identical to before — Unix socket, local bot spawn.

## [0.7.0] 2026-04-28 — `/summary` slash command for daily project usage

**Changed:** `hooks/discord_bot.py`, `tests/test_discord_bot.py`, `hooks/pyproject.toml`
**Why:** Users wanted a way to see how much they used Claude Code per project on a given day — project name, tokens, model breakdown, and session time — without manually scanning through session files.
**What:**
- **`/summary [date]` slash command:** Cross-references `~/.claude/history.jsonl` with per-session JSONL conversation logs to aggregate per-project token usage and session duration for a given UTC date (defaults to today). Posts the formatted report as a new forum post in `DISCORD_SUMMARY_CHANNEL_ID`.
- **`summarize_usage()`:** Core aggregation function — scans history to find sessions on the target date, deduplicates by `(project, sessionId)`, parses JSONL files to extract per-model token usage and message timestamps, and sums by project.
- **Helper functions:** `_format_duration()` (ms to human-readable), `_format_number()` (comma separators), `_build_summary_text()` (Discord markdown formatting).
- **New env var:** `DISCORD_SUMMARY_CHANNEL_ID` (falls back to `DISCORD_INSPECT_CHANNEL_ID`).
- **Tests:** 7 new unit tests for all helper functions and `summarize_usage()`.

## [0.6.1] 2026-04-27 — Fix long code block split causing unescaped underscores in Discord

**Changed:** `hooks/notify_discord.py`, `tests/test_notify_discord.py`
**Why:** When a fenced code block was too long and its closing \`\`\` was beyond the split window, `split_text()` would leave it open. Content like `x86_64-linux` in the next chunk appeared outside a code block, causing Discord to render `_64_` as italic.
**What:**
- **Long code block fence closure:** `split_text()` now closes the fence before the split point and reopens with the language specifier in the next chunk, preventing mid-block content from leaking into Discord's markdown parser
- **`_extract_fence_lang()`:** New helper to preserve the language tag (e.g., `python`) when reopening a code block after a split
- **Test coverage:** Added tests for `_extract_fence_lang`, balanced fences across chunks, fence lang preservation, and underscore escaping (`x86_64-linux` case)

## [0.6.0] 2026-04-27 — Native markdown rendering, fence-aware text splitting

**Changed:** `hooks/notify_discord.py`
**Why:** `_wrap_plan_for_discord()` wrapped plan content's markdown in ` ```markdown ` code blocks, preventing Discord from rendering it natively. `split_text()` could break inside ` ``` ` code blocks, leaving orphaned fences across Discord messages.
**What:**
- **Native markdown rendering:** Removed `_wrap_plan_for_discord()` — plan content is now sent as raw markdown, letting Discord render headers, bold, lists, and code blocks natively
- **Fence-aware text splitting:** `split_text()` no longer splits inside ` ``` ` fenced code blocks — backs up to before the opening fence (or extends to include the closing fence if the block starts near the split point)
- **Dead code removal:** `_wrap_plan_for_discord()` function removed

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
