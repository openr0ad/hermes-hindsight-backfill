#!/usr/bin/env python3
"""Backfill Hermes sessions into Hindsight long-term memory.

Reads Hermes sessions from ~/.hermes/state.db, filters to substantive
sessions (min exchanges, contains tool calls or code, not matching
exclusion patterns), formats conversation text, and retains via
hindsight_client.

Usage:
    uv run backfill_hindsight.py --dry-run --show-text --limit 3

    uv run backfill_hindsight.py --days 90 --min-turns 6

    uv run backfill_hindsight.py --url http://localhost:8888
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    from hindsight_client import Hindsight
except ImportError:
    print("ERROR: hindsight_client not installed. Run: uv pip install hindsight-all")
    sys.exit(1)

DEFAULT_DB = Path.home() / ".hermes" / "state.db"
DEFAULT_BANK_ID = "hermes"
TAG = "backfill"

DEFAULT_EXCLUDE_PATTERNS = [
    r"quick question",
    r"\bweather\b",
    r"\bbreakfast\b",
    r"\blunch\b",
    r"\bdinner\b",
    r"\bmacros?\b",
    r"\blogge?d?\b",
    r"\bnutrition\b",
    r"\bgreeting\b",
]


# ---------------------------------------------------------------------------
# State file — tracks session IDs already retained so re-runs skip them
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    try:
        return set(json.loads(state_path.read_text()))
    except Exception:
        return set()


def save_state(state_path: Path, done: set[str]) -> None:
    state_path.write_text(json.dumps(sorted(done)))


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_sessions(db_path: Path, min_turns: int, days: int) -> list[dict]:
    min_messages = max(min_turns * 2, min_turns)
    cutoff = datetime.now().timestamp() - (days * 86400)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, title, started_at, message_count, tool_call_count,
               display_name, profile_name
        FROM sessions
        WHERE archived = 0
          AND message_count >= ?
          AND started_at >= ?
          AND session_key IS NOT NULL
        ORDER BY started_at DESC
        """,
        (min_messages, cutoff),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_session_messages(db_path: Path, session_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT role, content, tool_calls, tool_name, timestamp
        FROM messages
        WHERE session_id = ?
          AND active = 1
          AND compacted = 0
          AND role IN ('user', 'assistant', 'system')
        ORDER BY timestamp ASC
        """,
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

def matches_exclusion(session: dict, patterns: list[re.Pattern]) -> str | None:
    title = session.get("title") or session.get("display_name") or ""
    for pat in patterns:
        if pat.search(title):
            return pat.pattern
    return None


def is_substantive(session: dict, messages: list[dict], min_tool_calls: int) -> bool:
    """Must have tool calls or code blocks — pure chat sessions are skipped."""
    if (session.get("tool_call_count") or 0) >= min_tool_calls:
        return True
    for msg in messages:
        if "```" in (msg.get("content") or ""):
            return True
    return False


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_conversation(messages: list[dict], session: dict) -> str:
    lines = []
    title = session.get("title") or session.get("display_name") or session["id"]
    started = datetime.fromtimestamp(session["started_at"]).strftime("%Y-%m-%d %H:%M")
    lines.append(f"Session: {title} ({started})")
    lines.append(f"Turns: {session['message_count']}, Tool calls: {session['tool_call_count']}")
    lines.append("")

    skip_system = True
    # Skip the first system message (the Hermes system prompt) but keep
    # subsequent system messages (injected context, instructions from tools).
    for msg in messages:
        role = msg["role"]
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls")

        if role == "system":
            if skip_system:
                skip_system = False
                continue
            lines.append(f"System: {content[:500]}")
            continue

        if role == "user":
            lines.append(f"User: {content[:2000]}")
        elif role == "assistant":
            if tool_calls:
                try:
                    calls = json.loads(tool_calls)
                    names = [c.get("function", {}).get("name", c.get("name", "?")) for c in calls]
                    lines.append(f"Assistant: [tool calls: {', '.join(names)}]")
                except Exception:
                    lines.append("Assistant: [tool call]")
            if content:
                lines.append(f"Assistant: {content[:2000]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Retain
# ---------------------------------------------------------------------------

def retain_session(client: Hindsight, session_id: str, text: str,
                    bank_id: str, dry_run: bool) -> bool:
    if dry_run:
        return True
    try:
        client.retain(
            bank_id=bank_id,
            content=text,
            tags=[TAG, f"session:{session_id[:8]}"],
            retain_async=False,  # sync avoids aiohttp unclosed-connection warnings
        )
        return True
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Backfill Hermes sessions into Hindsight")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to Hermes state.db")
    ap.add_argument("--url", type=str, default="http://localhost:8888", help="Hindsight API base URL")
    ap.add_argument("--bank-id", type=str, default=DEFAULT_BANK_ID, help="Hindsight memory bank ID")
    ap.add_argument("--min-turns", type=int, default=6, help="Minimum exchanges (user+assistant pairs)")
    ap.add_argument("--days", type=int, default=90, help="Look back N days")
    ap.add_argument("--limit", type=int, default=0, help="Max sessions to process (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing to Hindsight")
    ap.add_argument("--show-text", action="store_true", help="Print formatted text preview (dry-run only)")
    ap.add_argument("--no-skip-tagged", action="store_true", help="Re-process sessions already retained in a prior run")
    ap.add_argument("--state-file", type=Path, default=None, help="Ledger of completed session IDs (default: <db-dir>/.backfill_state.json)")
    ap.add_argument("--min-tool-calls", type=int, default=1, help="Min tool calls to count as substantive")
    ap.add_argument("--exclude-pattern", action="append", default=[], help="Regex to exclude by title (repeatable)")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"DB not found: {args.db}")
        sys.exit(1)

    state_path = args.state_file or (args.db.parent / ".backfill_state.json")
    # Always load state; --skip-tagged controls whether we act on it
    done = load_state(state_path)

    exclude_patterns = [
        re.compile(p, re.IGNORECASE) for p in DEFAULT_EXCLUDE_PATTERNS + args.exclude_pattern
    ]

    print(f"DB: {args.db}")
    print(f"Min turns (exchanges): {args.min_turns}, Days: {args.days}, Dry run: {args.dry_run}")
    print(f"State file: {state_path} ({len(done)} already retained)")

    sessions = get_db_sessions(args.db, args.min_turns, args.days)
    print(f"Found {len(sessions)} sessions passing turn/date filter")

    if args.limit:
        sessions = sessions[: args.limit]

    if not sessions:
        print("Nothing to do.")
        return

    client = None
    if not args.dry_run:
        client = Hindsight(base_url=args.url)

    ok = fail = skipped = 0
    for i, s in enumerate(sessions, 1):
        sid = s["id"]
        title = s.get("title") or s.get("display_name") or sid[:12]
        print(f"[{i}/{len(sessions)}] {title} ({s['message_count']} msgs)", end=" ... ")

        if not args.no_skip_tagged and sid in done:
            print("skip (already retained)")
            skipped += 1
            continue

        if s["message_count"] <= 2:
            print("skip (single-exchange)")
            skipped += 1
            continue

        exc_match = matches_exclusion(s, exclude_patterns)
        if exc_match:
            print(f"skip (matches exclusion: {exc_match!r})")
            skipped += 1
            continue

        msgs = get_session_messages(args.db, sid)
        if not msgs:
            print("skip (no messages)")
            skipped += 1
            continue

        if not is_substantive(s, msgs, args.min_tool_calls):
            print("skip (no tool calls or code — chat only)")
            skipped += 1
            continue

        text = format_conversation(msgs, s)
        if len(text.strip()) < 200:
            print("skip (too short)")
            skipped += 1
            continue

        if retain_session(client, sid, text, args.bank_id, args.dry_run):
            print("retained" if not args.dry_run else "would retain")
            if args.show_text and args.dry_run:
                print("  ---")
                print("  " + text[:400].replace("\n", "\n  "))
                print("  ---")
            ok += 1
            done.add(sid)
        else:
            fail += 1

    # Always write state after a real run so future --skip-tagged runs work
    if not args.dry_run:
        save_state(state_path, done)

    print(f"\nDone. retained={ok} failed={fail} skipped={skipped}")




def cli():
    """Entry point for `uv tool install` / `pip install` console_scripts."""
    main()


if __name__ == "__main__":
    main()
