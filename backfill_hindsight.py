#!/usr/bin/env python3
"""Backfill Hermes sessions into Hindsight long-term memory.

Reads Hermes sessions from ~/.hermes/state.db, filters to substantive
sessions (min exchanges, contains tool calls or code, not matching
exclusion patterns), formats conversation text, and retains via
hindsight_client.

This module is both the CLI and the importable core used by webui.py:
evaluate_session() runs the whole filter chain for one session and
build_retain_payload()/retain_payload() are the single retain path.

Usage:
    uv run backfill_hindsight.py --dry-run --show-text --limit 3

    uv run backfill_hindsight.py --days 90 --min-turns 6

    uv run backfill_hindsight.py --url http://localhost:8888
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    from hindsight_client import Hindsight
except ImportError:
    print("ERROR: hindsight_client not installed. Run: uv pip install hindsight-client")
    sys.exit(1)

# The DB path and API URL come from the environment so nothing host-specific is
# baked into source. HINDSIGHT_API_URL is the same variable the Hindsight Hermes
# plugin uses; set it to http://<host>:8888 when Hindsight runs elsewhere.
DEFAULT_DB = Path(os.path.expanduser(os.environ.get("HERMES_DB") or "~/.hermes/state.db"))
DEFAULT_BANK_ID = os.environ.get("HINDSIGHT_BANK") or "hermes"
DEFAULT_API_URL = os.environ.get("HINDSIGHT_API_URL") or "http://localhost:8888"
TAG = "backfill"
SOURCE = "hermes-hindsight-backfill"
MIN_TEXT_CHARS = 200

DEFAULT_EXCLUDE_PATTERNS = [
    r"quick question",
    r"\bweather\b",
    r"\bbreakfast\b",
    r"\blunch\b",
    r"\bdinner\b",
    r"\bsnacks?\b",
    r"\bmacros?\b",
    r"\bcalories\b",
    r"\bnutrition\b",
    r"\bgreeting\b",
    # Meal-log vocabulary. This only sees the session TITLE, so it is a blunt
    # instrument — a technical session that merely mentions "logs" (e.g.
    # "Review peanut logs") must NOT match. DEFAULT_EXCLUDE_TOOLS is the precise
    # signal; these patterns are the fallback for older sessions that logged
    # meals without going through the healthlog plugin.
    r"\bmeals?\b",
    r"\bmeal log(?:ging|ged)?\b",
    r"\bfood (?:log|intake|tracker|diary)\b",
    r"\bintake\b",
]

# Tools whose presence in a session marks it as a write-only log entry rather
# than technical work worth remembering. Matched against messages.tool_name,
# which is far more reliable than title heuristics. healthlog's log_meal is the
# meal tracker; add more with --exclude-tool (e.g. log_peptide).
DEFAULT_EXCLUDE_TOOLS = ["log_meal"]

# Titles that look like engineering work are never excluded, even if they
# mention food or logging. "Rebuilding meals.csv after write error" and
# "Improving AI meal logging prompts" are sessions about the meal-logging
# *system* — real technical work — not meal entries. Checked before the
# exclusion patterns; --keep-pattern adds to this list.
DEFAULT_KEEP_PATTERNS = [
    r"\bfix\b",
    r"\brebuil",
    r"\bdebug",
    r"\bprompt",
    r"\bprotocol\b",
    r"\bintegration\b",
    r"\.csv\b",
    r"\berror\b",
    r"\bscript",
    r"\bbug\b",
    r"\bskill\b",
    r"\bschema\b",
    r"\bcron\b",
]


# ---------------------------------------------------------------------------
# State file — tracks session IDs already retained so CLI re-runs skip them
# (CLI only; the web UI checks Hindsight itself instead)
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

def parse_date_arg(value: str) -> float:
    """Parse a --until value into a unix timestamp.

    Accepts YYYY-MM-DD (interpreted as *end* of that local day, inclusive) or
    an ISO-ish datetime (YYYY-MM-DD[ T]HH:MM[:SS]).
    """
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.timestamp()
    raise argparse.ArgumentTypeError(
        f"unrecognized date {value!r} — use YYYY-MM-DD or 'YYYY-MM-DD HH:MM[:SS]'"
    )


def connect_db(db_path: Path) -> sqlite3.Connection:
    """Open the Hermes DB read-only. This tool never writes to it."""
    uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_SESSION_COLUMNS = """id, title, started_at, message_count, tool_call_count,
                      display_name, profile_name, session_key"""


def get_db_sessions(conn: sqlite3.Connection, min_turns: int, days: int,
                    until: float | None = None,
                    exclude_missing_session_key: bool = False) -> list[dict]:
    """Sessions in the date window with at least min_turns exchanges.

    Pass min_turns=0 to get every session in the window (the web UI does this
    so short sessions are listed with a reason rather than hidden).
    """
    min_messages = max(min_turns * 2, min_turns)
    cutoff = datetime.now().timestamp() - (days * 86400)
    sql = f"""
        SELECT {_SESSION_COLUMNS}
        FROM sessions
        WHERE archived = 0
          AND message_count >= ?
          AND started_at >= ?
    """
    params: list = [min_messages, cutoff]
    # By default NULL session_key rows ARE included. session_key is only
    # populated for sessions that ended cleanly, so excluding NULLs silently
    # drops a large share of otherwise-good sessions (e.g. anything cut short
    # by a crash or gateway restart). Opt in to the stricter behaviour with
    # --exclude-missing-session-key.
    if exclude_missing_session_key:
        sql += " AND session_key IS NOT NULL"
    if until is not None:
        sql += " AND started_at <= ?"
        params.append(until)
    sql += " ORDER BY started_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute(f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?",
                       (session_id,)).fetchone()
    return dict(row) if row else None


def get_session_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
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
    return [dict(r) for r in rows]


def get_session_tools(conn: sqlite3.Connection, session_id: str) -> set[str]:
    """Distinct tool names invoked in a session.

    Deliberately separate from get_session_messages: that only returns
    user/assistant/system rows, while tool invocations are recorded as
    role='tool' rows carrying tool_name.
    """
    rows = conn.execute(
        "SELECT DISTINCT tool_name FROM messages "
        "WHERE session_id = ? AND tool_name IS NOT NULL",
        (session_id,),
    ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def session_title(session: dict) -> str:
    return session.get("title") or session.get("display_name") or ""


def matches_exclusion(session: dict, patterns: list[re.Pattern]) -> str | None:
    title = session_title(session)
    for pat in patterns:
        if pat.search(title):
            return pat.pattern
    return None


def matches_keep(session: dict, patterns: list[re.Pattern]) -> str | None:
    """Engineering-looking titles that override the exclusion patterns."""
    title = session_title(session)
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


@dataclass
class FilterOptions:
    """Every knob of the filter chain. CLI flags and web UI inputs map onto this."""
    min_turns: int = 6
    days: int = 90
    until: float | None = None
    exclude_missing_session_key: bool = False
    min_tool_calls: int = 1
    extra_exclude_patterns: list[str] = field(default_factory=list)
    extra_keep_patterns: list[str] = field(default_factory=list)
    exclude_tools: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDE_TOOLS))

    def __post_init__(self) -> None:
        self.exclude_re = [re.compile(p, re.IGNORECASE)
                           for p in DEFAULT_EXCLUDE_PATTERNS + list(self.extra_exclude_patterns)]
        self.keep_re = [re.compile(p, re.IGNORECASE)
                        for p in DEFAULT_KEEP_PATTERNS + list(self.extra_keep_patterns)]


@dataclass
class Reason:
    code: str      # stable machine-readable id, e.g. "excluded_tool"
    message: str   # human-readable text (matches the CLI's "skip (...)" wording)


@dataclass
class Verdict:
    session: dict
    reasons: list[Reason]          # why the filters exclude it, in pipeline order
    notes: list[str]               # informational, e.g. keep-pattern override
    tools: set[str]
    text: str | None               # formatted retain text (None if no messages)

    @property
    def included(self) -> bool:
        return not self.reasons


def evaluate_session(conn: sqlite3.Connection, session: dict,
                     opts: FilterOptions) -> Verdict:
    """Run the full filter chain on one session.

    Unlike the original CLI loop this does not stop at the first failing
    check: every reason is collected so the web UI can show all of them. The
    order matches the CLI pipeline, so reasons[0] is what the CLI reports.
    """
    sid = session["id"]
    reasons: list[Reason] = []
    notes: list[str] = []

    if (session.get("message_count") or 0) < opts.min_turns * 2:
        reasons.append(Reason("too_few_turns",
                              f"fewer than {opts.min_turns} exchanges "
                              f"({session.get('message_count') or 0} msgs)"))
    if opts.exclude_missing_session_key and not session.get("session_key"):
        reasons.append(Reason("missing_session_key", "no session_key (did not end cleanly)"))

    if (session.get("message_count") or 0) <= 2:
        reasons.append(Reason("single_exchange", "single-exchange"))

    keep_match = matches_keep(session, opts.keep_re)
    exc_match = matches_exclusion(session, opts.exclude_re)
    if exc_match and keep_match:
        notes.append(f"title matches exclusion '{exc_match}' but keep pattern "
                     f"'{keep_match}' overrides it")
    elif exc_match:
        reasons.append(Reason("excluded_title", f"matches exclusion: '{exc_match}'"))

    msgs = get_session_messages(conn, sid)
    if not msgs:
        reasons.append(Reason("no_messages", "no messages"))

    tools = get_session_tools(conn, sid)
    used = tools & opts.exclude_tools
    if used and keep_match:
        notes.append(f"used excluded tool {', '.join(sorted(used))} but keep pattern "
                     f"'{keep_match}' overrides it")
    elif used:
        reasons.append(Reason("excluded_tool", f"used excluded tool: {', '.join(sorted(used))}"))

    text = None
    if msgs:
        if not is_substantive(session, msgs, opts.min_tool_calls):
            reasons.append(Reason("not_substantive", "no tool calls or code — chat only"))
        text = format_conversation(msgs, session)
        if len(text.strip()) < MIN_TEXT_CHARS:
            reasons.append(Reason("too_short", "too short"))

    return Verdict(session=session, reasons=reasons, notes=notes, tools=tools, text=text)


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

def backfill_document_id(session_id: str) -> str:
    """Hindsight document id for a backfilled session.

    Deliberately NOT the bare session id: the Hindsight Hermes plugin uses the
    bare session id as its own document id (with update_mode=append), and a
    retain with the same document id would replace that live document.
    """
    return f"{TAG}:{session_id}"


def build_retain_payload(session: dict, text: str, bank_id: str) -> dict:
    """Exactly what retain_payload() sends. Shown verbatim by dry runs.

    Identity follows the Hindsight Hermes plugin's convention — a full
    ``session:<id>`` tag and ``session_id`` metadata — so one duplicate check
    matches sessions captured live and sessions backfilled by this tool.
    """
    sid = session["id"]
    return {
        "bank_id": bank_id,
        "document_id": backfill_document_id(sid),
        "content": text,
        "tags": [TAG, f"session:{sid}"],
        "metadata": {"session_id": sid, "source": SOURCE},
    }


def retain_payload(client: Hindsight, payload: dict) -> None:
    """Send one payload. Raises on failure."""
    client.retain(
        bank_id=payload["bank_id"],
        content=payload["content"],
        document_id=payload["document_id"],
        tags=payload["tags"],
        metadata=payload["metadata"],
        retain_async=False,  # sync avoids aiohttp unclosed-connection warnings
    )


def make_client(url: str, api_key: str | None = None) -> Hindsight:
    return Hindsight(base_url=url, api_key=api_key or os.environ.get("HINDSIGHT_API_KEY") or None)


# ---------------------------------------------------------------------------
# Connectivity checks and duplicate detection (read-only Hindsight calls)
# ---------------------------------------------------------------------------

def _api_get(url: str, path: str, params: dict | None = None,
             api_key: str | None = None, timeout: float = 15.0) -> dict:
    full = url.rstrip("/") + path
    if params:
        full += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(full, headers={"Accept": "application/json"})
    key = api_key or os.environ.get("HINDSIGHT_API_KEY")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # Typical cause: the URL points at the Control Plane UI, not the API.
        raise ValueError(f"{full} did not return JSON — is this the Hindsight API port?")


def check_db(db_path: Path) -> dict:
    """Is the Hermes DB present and readable?"""
    p = Path(db_path).expanduser()
    if not p.exists():
        return {"ok": False, "path": str(p), "error": "file not found"}
    try:
        conn = connect_db(p)
        try:
            n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            conn.close()
        return {"ok": True, "path": str(p), "sessions": n}
    except Exception as exc:
        return {"ok": False, "path": str(p), "error": str(exc)}


def check_hindsight(url: str, bank_id: str, api_key: str | None = None) -> dict:
    """Is the Hindsight API reachable, and does the bank exist?"""
    out: dict = {"url": url, "bank": bank_id}
    try:
        v = _api_get(url, "/version", api_key=api_key, timeout=5)
        out["version"] = v.get("api_version")
    except urllib.error.HTTPError as exc:
        hint = " — is this the Hindsight API port (not the Control Plane UI)?" if exc.code == 404 else ""
        return {**out, "ok": False, "error": f"HTTP {exc.code}{hint}"}
    except Exception as exc:
        return {**out, "ok": False, "error": str(exc)}
    try:
        d = _api_get(url, f"/v1/default/banks/{urllib.parse.quote(bank_id)}/documents",
                     {"limit": 0}, api_key=api_key, timeout=10)
        out["documents"] = d.get("total")
        out["ok"] = True
    except urllib.error.HTTPError as exc:
        out["ok"] = False
        out["error"] = f"bank {bank_id!r}: HTTP {exc.code}"
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out


@dataclass
class BankSessionIndex:
    """Which Hermes session ids already have a document in the bank."""
    exact: set[str]                 # full session ids (tags, metadata, document ids)
    legacy_backfill_days: set[str]  # 8-char prefixes from pre-fix backfill tags
    documents_scanned: int


# Old backfills tagged only session_id[:8], which for Hermes ids is the date
# (YYYYMMDD). Such a tag can't identify a session, only a day.
_LEGACY_PREFIX_LEN = 8


def fetch_bank_session_index(url: str, bank_id: str, api_key: str | None = None,
                             page_size: int = 500) -> BankSessionIndex:
    """List every document in the bank (read-only) and collect session ids."""
    exact: set[str] = set()
    legacy: set[str] = set()
    offset = scanned = 0
    path = f"/v1/default/banks/{urllib.parse.quote(bank_id)}/documents"
    while True:
        page = _api_get(url, path, {"limit": page_size, "offset": offset},
                        api_key=api_key, timeout=30)
        items = page.get("items") or []
        for doc in items:
            scanned += 1
            tags = doc.get("tags") or []
            meta = doc.get("document_metadata") or {}
            if meta.get("session_id"):
                exact.add(meta["session_id"])
            doc_id = doc.get("id") or ""
            if doc_id.startswith(f"{TAG}:"):
                exact.add(doc_id[len(TAG) + 1:])
            is_backfill = TAG in tags
            for t in tags:
                if not t.startswith("session:"):
                    continue
                val = t[len("session:"):]
                if is_backfill and len(val) == _LEGACY_PREFIX_LEN and not meta.get("session_id"):
                    legacy.add(val)
                else:
                    exact.add(val)
        if len(items) < page_size:
            break
        offset += page_size
    return BankSessionIndex(exact=exact, legacy_backfill_days=legacy, documents_scanned=scanned)


def duplicate_status(session_id: str, index: BankSessionIndex) -> str | None:
    """'exact' = a document for this session exists; 'possible' = an old,
    pre-fix backfill exists for a session on the same day; None = not found."""
    if session_id in index.exact:
        return "exact"
    if session_id[:_LEGACY_PREFIX_LEN] in index.legacy_backfill_days:
        return "possible"
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Backfill Hermes sessions into Hindsight")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to Hermes state.db (default: $HERMES_DB or ~/.hermes/state.db)")
    ap.add_argument("--url", type=str, default=DEFAULT_API_URL, help="Hindsight API base URL (default: $HINDSIGHT_API_URL or http://localhost:8888)")
    ap.add_argument("--bank-id", type=str, default=DEFAULT_BANK_ID, help="Hindsight memory bank ID (default: $HINDSIGHT_BANK or hermes)")
    ap.add_argument("--min-turns", type=int, default=6, help="Minimum exchanges (user+assistant pairs)")
    ap.add_argument("--days", type=int, default=90, help="Look back N days")
    ap.add_argument("--until", type=parse_date_arg, default=None,
                    help="Only include sessions started on/before this date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM'). "
                         "Upper bound — lets you target a specific window with --days.")
    ap.add_argument("--limit", type=int, default=0, help="Max sessions to process (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing to Hindsight")
    ap.add_argument("--show-text", action="store_true", help="Print formatted text preview (dry-run only)")
    ap.add_argument("--no-skip-tagged", action="store_true", help="Re-process sessions already retained in a prior run")
    ap.add_argument("--exclude-missing-session-key", action="store_true",
                    help="Only include sessions with a populated session_key. Default is to INCLUDE "
                         "sessions with a NULL session_key (they are usually good sessions that didn't "
                         "end cleanly) — pass this flag for the old, stricter behaviour.")
    ap.add_argument("--state-file", type=Path, default=None, help="Ledger of completed session IDs (default: <db-dir>/.backfill_state.json)")
    ap.add_argument("--min-tool-calls", type=int, default=1, help="Min tool calls to count as substantive")
    ap.add_argument("--exclude-pattern", action="append", default=[], help="Regex to exclude by title (repeatable)")
    ap.add_argument("--keep-pattern", action="append", default=[],
                    help="Regex that forces a title to be kept, overriding both the exclusion "
                         "patterns and tool exclusion (repeatable)")
    ap.add_argument("--exclude-tool", action="append", default=list(DEFAULT_EXCLUDE_TOOLS),
                    help="Exclude sessions that called this tool (repeatable; default: %(default)s). "
                         "Matched against messages.tool_name — precise, unlike title regexes.")
    ap.add_argument("--no-exclude-tools", action="store_true",
                    help="Disable tool-based exclusion entirely (keeps every session, including meal logs)")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"DB not found: {args.db}")
        sys.exit(1)

    state_path = args.state_file or (args.db.parent / ".backfill_state.json")
    # Always load state; --skip-tagged controls whether we act on it
    done = load_state(state_path)

    opts = FilterOptions(
        min_turns=args.min_turns,
        days=args.days,
        until=args.until,
        exclude_missing_session_key=args.exclude_missing_session_key,
        min_tool_calls=args.min_tool_calls,
        extra_exclude_patterns=args.exclude_pattern,
        extra_keep_patterns=args.keep_pattern,
        exclude_tools=set() if args.no_exclude_tools else set(args.exclude_tool),
    )

    print(f"DB: {args.db}")
    print(f"Min turns (exchanges): {args.min_turns}, Days: {args.days}, Dry run: {args.dry_run}")
    if args.until is not None:
        print(f"Until (inclusive): {datetime.fromtimestamp(args.until).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Hindsight URL: {args.url}")
    if opts.exclude_tools:
        print(f"Excluding sessions that called: {', '.join(sorted(opts.exclude_tools))}")
    print(f"State file: {state_path} ({len(done)} already retained)")

    conn = connect_db(args.db)
    sessions = get_db_sessions(conn, args.min_turns, args.days, args.until,
                               args.exclude_missing_session_key)
    print(f"Found {len(sessions)} sessions passing turn/date filter")

    if args.limit:
        sessions = sessions[: args.limit]

    if not sessions:
        print("Nothing to do.")
        return

    client = None
    if not args.dry_run:
        client = make_client(args.url)

    ok = fail = skipped = 0
    for i, s in enumerate(sessions, 1):
        sid = s["id"]
        title = session_title(s) or sid[:12]
        print(f"[{i}/{len(sessions)}] {title} ({s['message_count']} msgs)", end=" ... ")

        if not args.no_skip_tagged and sid in done:
            print("skip (already retained)")
            skipped += 1
            continue

        verdict = evaluate_session(conn, s, opts)
        if not verdict.included:
            print(f"skip ({verdict.reasons[0].message})")
            skipped += 1
            continue

        payload = build_retain_payload(s, verdict.text, args.bank_id)
        if args.dry_run:
            print("would retain")
            if args.show_text:
                print("  ---")
                print("  " + verdict.text[:400].replace("\n", "\n  "))
                print("  ---")
            ok += 1
            done.add(sid)
            continue
        try:
            retain_payload(client, payload)
        except Exception as exc:
            print(f"  FAIL: {exc}")
            fail += 1
            continue
        print("retained")
        ok += 1
        done.add(sid)

    conn.close()
    # Always write state after a real run so future --skip-tagged runs work
    if not args.dry_run:
        save_state(state_path, done)

    print(f"\nDone. retained={ok} failed={fail} skipped={skipped}")


def cli():
    """Entry point for `uv tool install` / `pip install` console_scripts."""
    main()


if __name__ == "__main__":
    main()
