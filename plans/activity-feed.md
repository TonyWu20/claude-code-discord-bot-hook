# Batched Activity Feed — Implementation Plan

## Context

The plugin currently monitors Claude Code via hooks but only shows the final message (Stop) or blocks for approvals. The user previously wired PostToolUse directly to Discord — it hit rate limits immediately because Claude makes dozens of tool calls per minute.

This plan adds a **rate-limited, batched activity feed** that surfaces tool execution in Discord without risking rate limits. The trade-off: ~5 second delay in exchange for safety, scannability, and zero interference with the approve/deny flow.

## Design

```
PostToolUse fires (async hook)
  → notify_discord.py --activity appends one JSON line to /tmp/claude_activity_<session>.jsonl
  → exits immediately (no IPC, no socket — pure file append)

discord_bot.py background task (every 5s)
  → reads new lines from buffer files (offset-tracked)
  → aggregates counts + recent commands + recent files
  → edits a single "Live Activity" message in the session's Discord thread
```

Key properties:
- **No IPC for activity events** — file append is ~100x faster than socket connect/send/read/close
- **O(1) per poll cycle** — offset tracking, never re-reads old data
- **Single message per session** — edited in-place, not new messages
- **Approve/deny path untouched** — uses different IPC type, remains immediate
- **POSIX append atomicity** — JSON lines <500 bytes, safe for concurrent shim writes

## Implementation Order

### 1. `hooks/hooks.json` — register PostToolUse + PostToolUseFailure

Add two entries after StopFailure:
```json
"PostToolUse": [{
  "hooks": [{
    "type": "command",
    "command": "~/.claude/hooks/.venv/bin/python ~/.claude/hooks/notify_discord.py --activity",
    "async": true
  }]
}],
"PostToolUseFailure": [/* same, async: true */]
```
- `async: true` → Claude doesn't wait, shim runs in background
- No matcher → fires for all tool calls

### 2. `hooks/notify_discord.py` — add `--activity` mode

Insert before the main event dispatch (~line 283), new branch:
```python
if "--activity" in sys.argv:
    data = json.loads(sys.stdin.read())
    session_id = data.get("session_id", "")
    label = resolve_session_label(session_id)
    entry = {
        "ts": time.time(),
        "tool": data.get("tool_name", ""),
        "event": data.get("hook_event_name", ""),
    }
    # Extract tool-specific summary fields only (never full content)
    tool_input = data.get("tool_input", {})
    if data.get("tool_name") == "Bash":
        entry["command"] = tool_input.get("command", "")[:200]
        entry["exit_code"] = data.get("tool_response", {}).get("exitCode")
    elif data.get("tool_name") in ("Read", "Edit", "Write"):
        entry["file"] = tool_input.get("file_path", "")[:150]
    if data.get("hook_event_name") == "PostToolUseFailure":
        entry["error"] = data.get("error", "")[:200]
    # Atomic append, no IPC, no ensure_bot_running()
    with open(f"/tmp/claude_activity_{label}.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
    sys.exit(0)
```

Also: add session_end sentinel write to the Stop handler (before line 296):
```python
if event == "Stop":
    try:
        with open(f"/tmp/claude_activity_{label}.jsonl", "a") as f:
            f.write(json.dumps({"type": "session_end", "ts": time.time()}) + "\n")
    except OSError: pass
    # ...existing Stop logic...
```

### 3. Test fixtures

Create 3 new fixtures in `hooks/tests/fixtures/`:
- `posttooluse_bash.json` — Bash PostToolUse with exit code
- `posttooluse_write.json` — Write PostToolUse with file path
- `posttooluse_failure.json` — Bash PostToolUseFailure with error

Update `FIXTURE_DESCRIPTIONS` in `simulate.py`.

### 4. `hooks/discord_bot.py` — background flush task

New constants (~line 46):
- `ACTIVITY_POLL_INTERVAL = 5` (seconds)
- `ACTIVITY_IDLE_TIMEOUT = 90` (seconds without activity before finalizing)
- `MAX_RECENT_BASH = 3`, `MAX_RECENT_FILES = 3`

New module-level state (~line 66): `_activity_state: dict[str, dict] = {}`

New functions (add before `run_socket_server`):
- **`flush_activity_buffers()`** — top-level async loop, runs every 5s after bot ready
- **`_poll_activity_buffers()`** — glob buffers, read new lines, aggregate, update messages
- **`_read_new_lines(path, offset)`** — seek to offset, parse new JSON lines, return (lines, new_offset)
- **`_aggregate_lines(state, lines)`** — increment counts, track recent bash/files, detect session_end
- **`_build_activity_text(state)`** — produce compact Discord markdown
- **`_create_activity_message(session, text)`** — post first activity message in thread
- **`_edit_activity_message(session, msg_id, text)`** — edit existing; return None if deleted
- **`_cleanup_stale_buffers()`** — delete buffer files with mtime > 1 hour (stale from dead bot)

Wire up in `on_ready()`: add `asyncio.create_task(flush_activity_buffers())`

### 5. Activity message format

```
🔄 **Live Activity** · 47 calls · 3 failed · 12m 30s
`Read`×15  `Edit`×8  `Bash`×7  `Write`×6  `Grep`×5  `Glob`×4

Recent Bash:
- `npm run build` [0]
- `pytest tests/ -x` [1]

Recent Files:
- `Edit` `src/models/user.ts`
- `Write` `tests/test_models.py`
```

Finalized (session end or idle timeout):
```
✅ **Session Activity** · 47 calls · 0 failed · 12m 30s
...
_Session complete. Total: 47 tool calls._
```

### 6. Tests

**`tests/test_discord_bot.py`** — unit tests for pure functions:
- `_build_activity_text` with data, empty, finalized
- `_read_new_lines` offset tracking, malformed line skipping
- `_aggregate_lines` counting, error tracking, session_end detection

**`tests/test_notify_discord.py`** — unit tests for `--activity` mode:
- Bash PostToolUse produces correct buffer entry
- PostToolUseFailure includes error field
- No IPC/ensure_bot_running called in activity mode
- Stop event writes session_end sentinel

### 7. Manual verification

```sh
# Start bot
cd hooks && python discord_bot.py

# Simulate rapid tool calls
for i in $(seq 1 10); do
  python tests/simulate.py --dry-run tests/fixtures/posttooluse_bash.json &
done

# Verify in Discord: single "Live Activity" message updates ~5s

# Cleanup
echo "done" > /tmp/claude_stop_<session>.txt
# Verify: message finalizes, buffer file deleted
```

## Files modified

| File | Change |
|---|---|
| `hooks/hooks.json` | Add PostToolUse, PostToolUseFailure (async) |
| `hooks/notify_discord.py` | Add `--activity` mode + session_end sentinel in Stop |
| `hooks/discord_bot.py` | Background flush task + aggregation + message edit functions |
| `hooks/tests/fixtures/*.json` | 3 new fixture files |
| `hooks/tests/simulate.py` | Update FIXTURE_DESCRIPTIONS |
| `tests/test_discord_bot.py` | Unit tests for aggregation/formatting functions |
| `tests/test_notify_discord.py` | Unit tests for --activity mode |
