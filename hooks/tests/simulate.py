#!/usr/bin/env python3
"""Test harness for simulating Claude Code hook events.

Usage:
    python tests/simulate.py fixtures/permission_request_bash.json
    python tests/simulate.py --dry-run fixtures/permission_request_bash.json
    python tests/simulate.py --list
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"
NOTIFY_SCRIPT = Path(__file__).parent.parent / "notify_discord.py"

FIXTURE_DESCRIPTIONS: dict[str, str] = {
    "permission_request_bash.json": "PermissionRequest for Bash with suggestions",
    "permission_request_write.json": "PermissionRequest for Write with suggestions",
    "pretooluse_askuserquestion.json": "PermissionRequest for AskUserQuestion",
    "pretooluse_exitplanmode.json": "PermissionRequest for ExitPlanMode",
    "pretooluse_bash.json": "PermissionRequest for Bash (no suggestions)",
    "notification.json": "Notification event (fire-and-forget)",
    "stop.json": "Stop with last_assistant_message",
    "subagent_stop.json": "SubagentStop with last_assistant_message",
    "user_prompt_submit.json": "UserPromptSubmit (idle watchdog trigger)",
}


def list_fixtures() -> None:
    print("Available fixtures:")
    for fname in sorted(FIXTURE_DESCRIPTIONS):
        path = FIXTURES_DIR / fname
        size = path.stat().st_size if path.exists() else 0
        print(f"  {fname:45s} {FIXTURE_DESCRIPTIONS[fname]:50s} ({size} bytes)")
    print()
    print("Usage:")
    print("  python tests/simulate.py <fixture_name>")
    print("  python tests/simulate.py --dry-run <fixture_name>")


def load_fixture(name: str) -> dict:
    # Strip any directory prefix (accept name.json or fixtures/name.json)
    name = Path(name).name
    path = FIXTURES_DIR / name
    if not path.exists():
        available = ", ".join(sorted(FIXTURE_DESCRIPTIONS))
        print(f"Fixture '{name}' not found. Available: {available}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text())


def dry_run(fixture: dict, name: str) -> None:
    print(f"=== Dry Run: {name} ===")
    print(f"Event: {fixture.get('hook_event_name', '?')}")
    print(f"Tool:   {fixture.get('tool_name', 'N/A')}")
    session_id = fixture.get("session_id", "?")
    print(f"Session: {session_id[:16]}{'...' if len(session_id) > 16 else ''}")

    print(f"\n-- tool_input --")
    tool_input = fixture.get("tool_input", {})
    print(json.dumps(tool_input, indent=2, ensure_ascii=False)[:3000])

    suggestions = fixture.get("permission_suggestions", [])
    if suggestions:
        print(f"\n-- permission_suggestions ({len(suggestions)}) --")
        for i, s in enumerate(suggestions):
            stype = s.get("type")
            behavior = s.get("behavior")
            dest = s.get("destination")
            print(f"  [{i}] type={stype} behavior={behavior} dest={dest}")
            for rule in s.get("rules", []):
                rc = rule.get("ruleContent", "")
                print(f"       {rule.get('toolName')} -> {rc or '(any)'}")

    print(f"\n-- hook_output would be --")
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "allow",
                        "reason": "Approved via Discord",
                    },
                }
            },
            indent=2,
        )
    )

    print(f"\n-- IPC message would be sent to bot --")
    print("(connect to Discord, post message with interactive buttons)")
    print()
    print("Dry run complete. No changes were made.")


def run_normal(fixture: dict, name: str) -> None:
    print(f"=== Running: {name} ===")
    result = subprocess.run(
        [sys.executable, str(NOTIFY_SCRIPT)],
        input=json.dumps(fixture),
        capture_output=True,
        text=True,
    )
    print(f"-- stdout (hook decision) --")
    print((result.stdout or "(empty)").strip())
    print(f"-- stderr --")
    print((result.stderr or "(empty)").strip())
    print(f"Exit code: {result.returncode}")
    if result.returncode != 0:
        print("(exit code 0 = allow/continue, exit code 2 = deny)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate Claude Code hook events")
    parser.add_argument("fixture", nargs="?", help="Fixture filename")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate without connecting to bot",
    )
    parser.add_argument(
        "--list", action="store_true", help="List available fixtures"
    )
    args = parser.parse_args()

    if args.list or not args.fixture:
        list_fixtures()
        return

    fixture = load_fixture(args.fixture)

    if args.dry_run:
        dry_run(fixture, args.fixture)
    else:
        run_normal(fixture, args.fixture)


if __name__ == "__main__":
    main()
