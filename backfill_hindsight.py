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
DEFAULT_HERMES_HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes"))
DEFAULT_DB = Path(os.path.expanduser(os.environ.get("HERMES_DB") or str(DEFAULT_HERMES_HOME / "state.db")))
DEFAULT_BANK_ID = os.environ.get("HINDSIGHT_BANK") or "hermes"
DEFAULT_API_URL = os.environ.get("HINDSIGHT_API_URL") or "http://localhost:8888"
TAG = "backfill"
SOURCE = "hermes-hindsight-backfill"
MIN_TEXT_CHARS = 200

@dataclass(frozen=True)
class TitleRule:
    """A built-in title rule: stable id (for --disable-rule / the UI), the
    regex, and a plain-language label shown to people instead of the regex."""
    id: str
    pattern: str
    label: str


# Title-based exclusions. These only see the session TITLE, so they are a blunt
# instrument — a technical session that merely mentions "logs" (e.g. "Review
# peanut logs") must NOT match. DEFAULT_EXCLUDE_TOOLS is the precise signal; the
# meal words are the fallback for older sessions that logged meals without
# going through the healthlog plugin.
EXCLUDE_RULES = [
    TitleRule("quick-question", r"quick question", '"quick question"'),
    TitleRule("weather", r"\bweather\b", '"weather"'),
    TitleRule("breakfast", r"\bbreakfast\b", '"breakfast"'),
    TitleRule("lunch", r"\blunch\b", '"lunch"'),
    TitleRule("dinner", r"\bdinner\b", '"dinner"'),
    TitleRule("snack", r"\bsnacks?\b", '"snack" or "snacks"'),
    TitleRule("macros", r"\bmacros?\b", '"macro" or "macros"'),
    TitleRule("calories", r"\bcalories\b", '"calories"'),
    TitleRule("nutrition", r"\bnutrition\b", '"nutrition"'),
    TitleRule("greeting", r"\bgreeting\b", '"greeting"'),
    TitleRule("meal", r"\bmeals?\b", '"meal" or "meals"'),
    TitleRule("meal-log", r"\bmeal log(?:ging|ged)?\b", '"meal log", "meal logging" or "meal logged"'),
    TitleRule("food-log", r"\bfood (?:log|intake|tracker|diary)\b",
              '"food log", "food intake", "food tracker" or "food diary"'),
    TitleRule("intake", r"\bintake\b", '"intake"'),
]
DEFAULT_EXCLUDE_PATTERNS = [r.pattern for r in EXCLUDE_RULES]

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
KEEP_RULES = [
    TitleRule("fix", r"\bfix\b", '"fix"'),
    TitleRule("rebuild", r"\brebuil", 'words starting with "rebuil" (rebuild, rebuilding, rebuilt)'),
    TitleRule("debug", r"\bdebug", 'words starting with "debug" (debug, debugging)'),
    TitleRule("prompt", r"\bprompt", 'words starting with "prompt" (prompt, prompts)'),
    TitleRule("protocol", r"\bprotocol\b", '"protocol"'),
    TitleRule("integration", r"\bintegration\b", '"integration"'),
    TitleRule("error", r"\berror\b", '"error"'),
    TitleRule("script", r"\bscript", 'words starting with "script" (script, scripts, scripting)'),
    TitleRule("bug", r"\bbug\b", '"bug"'),
    TitleRule("skill", r"\bskill\b", '"skill"'),
    TitleRule("schema", r"\bschema\b", '"schema"'),
    TitleRule("cron", r"\bcron\b", '"cron"'),
]
DEFAULT_KEEP_PATTERNS = [r.pattern for r in KEEP_RULES]


def word_pattern(text: str) -> str:
    """Plain words → regex matching them as whole words (case-insensitive at
    compile time). 'quick question' also matches 'quick   question'."""
    words = text.strip().split()
    return r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"


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


# A compiled title rule: (plain-language label, compiled regex).
CompiledRule = tuple[str, re.Pattern]


def _first_match(session: dict, rules: list[CompiledRule]) -> str | None:
    title = session_title(session)
    for label, pat in rules:
        if pat.search(title):
            return label
    return None


def matches_exclusion(session: dict, rules: list[CompiledRule]) -> str | None:
    """Label of the first exclusion rule matching the title, if any."""
    return _first_match(session, rules)


def matches_keep(session: dict, rules: list[CompiledRule]) -> str | None:
    """Engineering-looking titles that override the exclusion rules."""
    return _first_match(session, rules)


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
    min_turns: int = 5
    days: int = 90
    until: float | None = None
    exclude_missing_session_key: bool = False
    min_tool_calls: int = 1
    extra_exclude_patterns: list = field(default_factory=list)  # regexes or (label, regex)
    extra_keep_patterns: list = field(default_factory=list)     # regexes or (label, regex)
    exclude_tools: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDE_TOOLS))
    disabled_rules: set[str] = field(default_factory=set)  # TitleRule ids switched off

    def __post_init__(self) -> None:
        known = {r.id for r in EXCLUDE_RULES + KEEP_RULES}
        unknown = set(self.disabled_rules) - known
        if unknown:
            raise ValueError(f"unknown rule id(s): {', '.join(sorted(unknown))} "
                             f"(see --list-rules)")
        self.exclude_re = self._compile(EXCLUDE_RULES, self.extra_exclude_patterns)
        self.keep_re = self._compile(KEEP_RULES, self.extra_keep_patterns)

    def _compile(self, builtin: list[TitleRule], extra: list) -> list[CompiledRule]:
        rules = [(r.label, re.compile(r.pattern, re.IGNORECASE))
                 for r in builtin if r.id not in self.disabled_rules]
        # Extras are raw regexes, or (label, regex) pairs when the caller has a
        # friendlier label (the web UI's plain-word entries).
        for item in extra:
            label, pat = item if isinstance(item, tuple) else (f"/{item}/", item)
            rules.append((label, re.compile(pat, re.IGNORECASE)))
        return rules


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
    in_hindsight: str | None = None  # "exact" / "possible" / None (see duplicate_status)

    @property
    def included(self) -> bool:
        return not self.reasons


def evaluate_session(conn: sqlite3.Connection, session: dict,
                     opts: FilterOptions,
                     bank_index: BankSessionIndex | None = None,
                     bank_id: str | None = None) -> Verdict:
    """Run the full filter chain on one session.

    Unlike the original CLI loop this does not stop at the first failing
    check: every reason is collected so the web UI can show all of them. The
    order matches the CLI pipeline, so reasons[0] is what the CLI reports.

    With a bank_index (from fetch_bank_session_index), sessions the bank
    already holds are skipped too — that check comes first.
    """
    sid = session["id"]
    reasons: list[Reason] = []
    notes: list[str] = []

    in_hindsight = duplicate_status(sid, bank_index) if bank_index else None
    if in_hindsight == "exact":
        reasons.append(Reason("in_hindsight",
                              f"already in Hindsight bank '{bank_id}'" if bank_id
                              else "already in Hindsight"))
    elif in_hindsight == "possible":
        reasons.append(Reason("maybe_in_hindsight",
                              "possibly already in Hindsight: an old backfill from "
                              "the same day exists and may be this session"))

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
        notes.append(f"title mentions {exc_match}, but kept because it also "
                     f"mentions {keep_match}")
    elif exc_match:
        reasons.append(Reason("excluded_title", f"title mentions {exc_match}"))

    msgs = get_session_messages(conn, sid)
    if not msgs:
        reasons.append(Reason("no_messages", "no messages"))

    tools = get_session_tools(conn, sid)
    used = tools & opts.exclude_tools
    if used and keep_match:
        notes.append(f"used skipped tool {', '.join(sorted(used))}, but kept because "
                     f"the title mentions {keep_match}")
    elif used:
        reasons.append(Reason("excluded_tool", f"used excluded tool: {', '.join(sorted(used))}"))

    text = None
    if msgs:
        if not is_substantive(session, msgs, opts.min_tool_calls):
            reasons.append(Reason("not_substantive", "no tool calls or code — chat only"))
        text = format_conversation(msgs, session)
        if len(text.strip()) < MIN_TEXT_CHARS:
            reasons.append(Reason("too_short", "too short"))

    return Verdict(session=session, reasons=reasons, notes=notes, tools=tools, text=text,
                   in_hindsight=in_hindsight)


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

def build_retain_payload(session: dict, text: str, bank_id: str) -> dict:
    """Exactly what retain_payload() sends. Shown verbatim by previews.

    Identity follows the Hindsight Hermes plugin's convention — document id =
    session id, a ``session:<id>`` tag and ``session_id`` metadata — so a
    session has one document no matter which path stored it. The ``backfill``
    tag records where it came from.
    """
    sid = session["id"]
    return {
        "bank_id": bank_id,
        "document_id": sid,
        "content": text,
        "tags": [TAG, f"session:{sid}"],
        "metadata": {"session_id": sid, "source": SOURCE},
    }


class AlreadyInHindsight(Exception):
    """Refused: the bank already holds a document for this session."""

    def __init__(self, session_id: str, document_ids: list[str]):
        self.document_ids = document_ids
        super().__init__(f"already in Hindsight (document {', '.join(document_ids)}); not sent")


def find_session_documents(url: str, bank_id: str, session_id: str,
                           api_key: str | None = None) -> list[str]:
    """Ids of documents in the bank that belong to this session (read-only).

    Checks both the document id (the plugin's and ours) and the
    ``session:<id>`` tag (covers the plugin's older ``<id>-<timestamp>`` ids).
    """
    found: list[str] = []
    bank = urllib.parse.quote(bank_id, safe="")
    try:
        doc = _api_get(url, f"/v1/default/banks/{bank}/documents/"
                            f"{urllib.parse.quote(session_id, safe='')}", api_key=api_key)
        found.append(doc.get("id") or session_id)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    page = _api_get(url, f"/v1/default/banks/{bank}/documents",
                    {"tags": f"session:{session_id}", "tags_match": "any_strict", "limit": 50},
                    api_key=api_key)
    for d in page.get("items") or []:
        if d.get("id") and d["id"] not in found:
            found.append(d["id"])
    return found


def retain_new(client: Hindsight, payload: dict, url: str,
               api_key: str | None = None) -> None:
    """Retain only if the session isn't in the bank yet.

    Retaining with an existing document id would *replace* that document —
    for a live-captured session, its plugin document — so a session that is
    already present is refused with AlreadyInHindsight instead. To re-send a
    session, delete its document in Hindsight first.
    """
    sid = payload["metadata"]["session_id"]
    existing = find_session_documents(url, payload["bank_id"], sid, api_key)
    if existing:
        raise AlreadyInHindsight(sid, existing)
    retain_payload(client, payload)


def retain_payload(client: Hindsight, payload: dict) -> None:
    """Send one payload unconditionally. Raises on failure. Prefer retain_new()."""
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
# Hermes profiles → session DB and Hindsight bank
# ---------------------------------------------------------------------------
#
# Each Hermes profile has its own home: the default profile is HERMES_HOME
# itself (~/.hermes), others live in HERMES_HOME/profiles/<name>. Each home has
# its own state.db, and the Hindsight Hermes plugin picks the bank from that
# home's hindsight/config.json (falling back to the shared, legacy
# ~/.hindsight/config.json). We mirror that so backfilled sessions land in the
# bank the profile's live sessions go to.

DEFAULT_PROFILE = "default"


@dataclass
class Profile:
    name: str
    home: Path
    db: Path
    bank: str | None          # None = can't tell; the user must choose
    bank_source: str          # where the bank came from, for display
    warning: str | None = None


def resolve_profile_bank(name: str, home: Path) -> tuple[str | None, str, str | None]:
    """(bank, source, warning) the Hindsight Hermes plugin would use for a profile."""
    for cfg in (home / "hindsight" / "config.json", Path.home() / ".hindsight" / "config.json"):
        if not cfg.is_file():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            continue  # the plugin also skips unreadable/empty files
        if not data:
            continue
        template = data.get("bank_id_template") or ""
        if template:
            placeholders = set(re.findall(r"\{(\w+)\}", template))
            if placeholders <= {"profile"}:
                return template.replace("{profile}", name), f"{cfg} (template)", None
            return None, str(cfg), (f"bank template {template!r} depends on each session "
                                    f"({', '.join(sorted(placeholders))}); choose a bank")
        return data.get("bank_id") or data.get("bankId") or "hermes", str(cfg), None
    return None, "", "this profile has no Hindsight config; choose a bank"


def discover_profiles(hermes_home: Path = DEFAULT_HERMES_HOME) -> list[Profile]:
    """Profiles that have a session database, default first."""
    home = Path(hermes_home).expanduser()
    homes = [(DEFAULT_PROFILE, home)]
    pdir = home / "profiles"
    if pdir.is_dir():
        homes += sorted((d.name, d) for d in pdir.iterdir() if d.is_dir())
    out = []
    for name, h in homes:
        db = h / "state.db"
        if db.is_file():
            bank, source, warning = resolve_profile_bank(name, h)
            out.append(Profile(name, h, db, bank, source, warning))
    return out


def get_profile(name: str, hermes_home: Path = DEFAULT_HERMES_HOME) -> Profile:
    for p in discover_profiles(hermes_home):
        if p.name == name:
            return p
    raise ValueError(f"no Hermes profile {name!r} with a state.db under {hermes_home}")


def list_banks(url: str, api_key: str | None = None) -> list[str]:
    """Bank ids that exist in Hindsight (read-only)."""
    data = _api_get(url, "/v1/default/banks", api_key=api_key, timeout=10)
    return [b["bank_id"] for b in data.get("banks") or [] if b.get("bank_id")]


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


def check_hindsight(url: str, bank_id: str | None, api_key: str | None = None) -> dict:
    """Is the Hindsight API reachable, and (if given) does the bank exist?"""
    out: dict = {"url": url, "bank": bank_id}
    try:
        v = _api_get(url, "/version", api_key=api_key, timeout=5)
        out["version"] = v.get("api_version")
    except urllib.error.HTTPError as exc:
        hint = " — is this the Hindsight API port (not the Control Plane UI)?" if exc.code == 404 else ""
        return {**out, "ok": False, "error": f"HTTP {exc.code}{hint}"}
    except Exception as exc:
        return {**out, "ok": False, "error": str(exc)}
    if not bank_id:
        return {**out, "ok": True}
    try:
        if bank_id not in list_banks(url, api_key):
            return {**out, "ok": False, "error": f"bank {bank_id!r} does not exist"}
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
            if doc.get("id"):
                exact.add(doc["id"])  # plugin and backfill both use the session id
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
    ap.add_argument("--profile", type=str, default=None,
                    help="Hermes profile to backfill: uses its state.db and the bank from its Hindsight "
                         "config (see --list-profiles). --db / --bank-id override either.")
    ap.add_argument("--list-profiles", action="store_true",
                    help="List Hermes profiles with their session DB and Hindsight bank, then exit")
    ap.add_argument("--hermes-home", type=Path, default=DEFAULT_HERMES_HOME,
                    help="Hermes home holding state.db and profiles/ (default: $HERMES_HOME or ~/.hermes)")
    ap.add_argument("--db", type=Path, default=None, help="Path to Hermes state.db (default: the profile's, else $HERMES_DB or ~/.hermes/state.db)")
    ap.add_argument("--url", type=str, default=DEFAULT_API_URL, help="Hindsight API base URL (default: $HINDSIGHT_API_URL or http://localhost:8888)")
    ap.add_argument("--bank-id", type=str, default=None, help="Hindsight memory bank ID (default: the profile's bank, else $HINDSIGHT_BANK or hermes)")
    ap.add_argument("--min-turns", type=int, default=5, help="Minimum exchanges (user+assistant pairs)")
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
    ap.add_argument("--disable-rule", action="append", default=[], metavar="RULE_ID",
                    help="Switch off a built-in title rule by id (repeatable; see --list-rules)")
    ap.add_argument("--list-rules", action="store_true",
                    help="List the built-in title rules with their ids and exit")
    args = ap.parse_args()

    if args.list_profiles:
        profiles = discover_profiles(args.hermes_home)
        if not profiles:
            print(f"No Hermes profiles with a state.db under {args.hermes_home}")
        for p in profiles:
            print(f"{p.name:<12} bank={p.bank or '?':<12} db={p.db}")
            if p.warning:
                print(f"{'':<12} ! {p.warning}")
        return

    if args.profile:
        try:
            prof = get_profile(args.profile, args.hermes_home)
        except ValueError as exc:
            ap.error(str(exc))
        args.db = args.db or prof.db
        if not args.bank_id:
            if not prof.bank:
                ap.error(f"profile {prof.name!r}: {prof.warning} (pass --bank-id)")
            args.bank_id = prof.bank
    args.db = args.db or DEFAULT_DB
    args.bank_id = args.bank_id or DEFAULT_BANK_ID

    if args.list_rules:
        for heading, rules in (("Skip sessions whose title mentions:", EXCLUDE_RULES),
                               ("Always keep sessions whose title mentions (overrides skips):", KEEP_RULES)):
            print(heading)
            for r in rules:
                print(f"  {r.id:<16} {r.label}")
        return

    state_path = args.state_file or (args.db.parent / ".backfill_state.json")
    # Always load state; --skip-tagged controls whether we act on it
    done = load_state(state_path)

    try:
        opts = FilterOptions(
            min_turns=args.min_turns,
            days=args.days,
            until=args.until,
            exclude_missing_session_key=args.exclude_missing_session_key,
            min_tool_calls=args.min_tool_calls,
            extra_exclude_patterns=args.exclude_pattern,
            extra_keep_patterns=args.keep_pattern,
            exclude_tools=set() if args.no_exclude_tools else set(args.exclude_tool),
            disabled_rules=set(args.disable_rule),
        )
    except (ValueError, re.error) as exc:
        ap.error(str(exc))

    if not args.db.exists():
        print(f"DB not found: {args.db}")
        sys.exit(1)

    print(f"DB: {args.db}")
    print(f"Min turns (exchanges): {args.min_turns}, Days: {args.days}, Dry run: {args.dry_run}")
    if args.until is not None:
        print(f"Until (inclusive): {datetime.fromtimestamp(args.until).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Hindsight URL: {args.url}  bank: {args.bank_id}")
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

    # Which sessions the bank already has (read-only). Those are skipped by
    # the rules; real sends are also guarded per session by retain_new().
    bank_index = None
    try:
        banks = list_banks(args.url)
        if args.bank_id in banks:
            bank_index = fetch_bank_session_index(args.url, args.bank_id)
            print(f"Hindsight bank {args.bank_id!r}: {bank_index.documents_scanned} documents checked")
        elif not args.dry_run:
            # Hindsight would create the bank on first write; refuse instead.
            print(f"Bank {args.bank_id!r} does not exist in Hindsight (have: {', '.join(banks)})")
            sys.exit(1)
        else:
            print(f"Note: bank {args.bank_id!r} does not exist in Hindsight")
    except Exception as exc:
        if not args.dry_run:
            print(f"Cannot reach Hindsight at {args.url}: {exc}")
            sys.exit(1)
        print(f"Note: could not check Hindsight ({exc}); sessions already there are not marked")

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

        verdict = evaluate_session(conn, s, opts, bank_index, args.bank_id)
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
            retain_new(client, payload, args.url)
        except AlreadyInHindsight as exc:
            print(f"skip ({exc})")
            skipped += 1
            done.add(sid)
            continue
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
