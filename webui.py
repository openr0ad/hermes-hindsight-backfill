#!/usr/bin/env python3
"""Lightweight local web UI for hermes-hindsight-backfill.

Single process, standard library HTTP server, no auth — meant for a LAN.
All filtering and retain logic lives in backfill_hindsight.py; this file only
exposes it as JSON endpoints plus one static page (webui.html).

Configuration comes from environment variables, optionally loaded from a
config.env file next to this script (or the file named by $WEBUI_CONFIG).
Real environment variables win over the file. See config.example.env.

Run:
    uv run webui.py            # then open http://<host>:8765/
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import traceback
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_config_file(path: Path) -> bool:
    """Load KEY=VALUE lines into os.environ without overriding real env vars."""
    if not path.is_file():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


# Must run before importing the core module: its defaults read the environment.
CONFIG_PATH = Path(os.environ.get("WEBUI_CONFIG") or HERE / "config.env").expanduser()
CONFIG_LOADED = load_config_file(CONFIG_PATH)

import backfill_hindsight as core  # noqa: E402

HERMES_HOME = core.DEFAULT_HERMES_HOME
API_URL = core.DEFAULT_API_URL
API_KEY = os.environ.get("HINDSIGHT_API_KEY") or None
HOST = os.environ.get("WEBUI_HOST") or "0.0.0.0"
PORT = int(os.environ.get("WEBUI_PORT") or 8765)
MAX_BODY = 1 << 20

# Retains are slow (server-side LLM extraction) and not worth racing; one at a time.
_commit_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Request handling helpers
# ---------------------------------------------------------------------------

class BadRequest(Exception):
    pass


def _filter_options(body: dict) -> core.FilterOptions:
    def num(name: str, default: int) -> int:
        try:
            return int(body.get(name, default))
        except (TypeError, ValueError):
            raise BadRequest(f"{name} must be a number")

    until = body.get("until") or None
    if until:
        try:
            until = core.parse_date_arg(str(until))
        except Exception as exc:
            raise BadRequest(str(exc))

    def str_list(name: str) -> list[str]:
        val = body.get(name) or []
        if not isinstance(val, list):
            raise BadRequest(f"{name} must be a list")
        return [str(v).strip() for v in val if str(v).strip()]

    # Custom title rules arrive as plain words (matched as whole words) unless
    # the user ticked "these are regular expressions".
    as_regex = bool(body.get("custom_is_regex"))
    def rules(name: str) -> list[str]:
        items = str_list(name)
        return items if as_regex else [(f'"{w}"', core.word_pattern(w)) for w in items]

    tools = body.get("exclude_tools")
    try:
        return core.FilterOptions(
            min_turns=num("min_turns", 6),
            days=num("days", 90),
            until=until,
            exclude_missing_session_key=bool(body.get("exclude_missing_session_key")),
            min_tool_calls=num("min_tool_calls", 1),
            extra_exclude_patterns=rules("custom_exclude"),
            extra_keep_patterns=rules("custom_keep"),
            exclude_tools=set(str_list("exclude_tools")) if tools is not None
            else set(core.DEFAULT_EXCLUDE_TOOLS),
            disabled_rules=set(str_list("disabled_rules")),
        )
    except re.error as exc:
        raise BadRequest(f"invalid regular expression: {exc}")
    except ValueError as exc:
        raise BadRequest(str(exc))


def _profile(body: dict) -> core.Profile:
    """The Hermes profile named in the request (only discovered profiles are allowed)."""
    try:
        return core.get_profile(str(body.get("profile") or core.DEFAULT_PROFILE), HERMES_HOME)
    except ValueError as exc:
        raise BadRequest(str(exc))


def _bank(body: dict) -> str:
    bank = str(body.get("bank") or "").strip()
    if not bank:
        raise BadRequest("choose a Hindsight bank first")
    return bank


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def list_sessions(body: dict) -> dict:
    opts = _filter_options(body)
    prof = _profile(body)
    bank = str(body.get("bank") or "").strip()
    # Ask Hindsight which sessions the bank already has, so "already in
    # Hindsight" is one of the rules. A failure here doesn't stop the list.
    index, hs = None, {"bank": bank or None, "checked": False}
    if bank:
        try:
            if bank not in core.list_banks(API_URL, API_KEY):
                hs["error"] = f"bank {bank!r} does not exist in Hindsight"
            else:
                index = core.fetch_bank_session_index(API_URL, bank, API_KEY)
                hs.update(checked=True, documents_scanned=index.documents_scanned,
                          checked_at=datetime.now().strftime("%H:%M:%S"))
        except Exception as exc:
            hs["error"] = f"could not check Hindsight: {exc}"
    conn = core.connect_db(prof.db)
    try:
        # min_turns=0: list short sessions too, with a reason, so they can be
        # selected manually. evaluate_session() applies the real threshold.
        sessions = core.get_db_sessions(conn, 0, opts.days, opts.until, False)
        out = []
        for s in sessions:
            v = core.evaluate_session(conn, s, opts, index, bank or None)
            out.append({
                "id": s["id"],
                "title": core.session_title(s) or s["id"],
                "started": _fmt_ts(s.get("started_at")),
                "message_count": s.get("message_count") or 0,
                "tool_call_count": s.get("tool_call_count") or 0,
                "tools": sorted(v.tools),
                "included": v.included,
                "reasons": [{"code": r.code, "message": r.message} for r in v.reasons],
                "notes": v.notes,
                "text_chars": len(v.text) if v.text else 0,
                "in_hindsight": v.in_hindsight,
            })
    finally:
        conn.close()
    return {"sessions": out, "count": len(out), "hindsight": hs,
            "included": sum(1 for s in out if s["included"]),
            "in_hindsight": sum(1 for s in out if s["in_hindsight"] == "exact")}


def _payload_for(conn, session_id: str, bank: str) -> dict:
    """Payload for one session, independent of filters (manual selection wins)."""
    s = core.get_session(conn, session_id)
    if not s:
        raise BadRequest(f"unknown session {session_id}")
    msgs = core.get_session_messages(conn, session_id)
    if not msgs:
        raise BadRequest("session has no messages; nothing to send")
    return core.build_retain_payload(s, core.format_conversation(msgs, s), bank)


def _ids(body: dict) -> list[str]:
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise BadRequest("ids must be a list of session ids")
    return ids


def preview(body: dict) -> dict:
    """Preview: build exactly what would be sent. Makes no Hindsight calls."""
    bank = _bank(body)
    conn = core.connect_db(_profile(body).db)
    items = []
    try:
        for sid in _ids(body):
            try:
                items.append({"id": sid, "ok": True, "payload": _payload_for(conn, sid, bank)})
            except BadRequest as exc:
                items.append({"id": sid, "ok": False, "error": str(exc)})
    finally:
        conn.close()
    return {"url": API_URL, "items": items}


def commit_one(body: dict) -> dict:
    sid = body.get("id")
    if not isinstance(sid, str):
        raise BadRequest("id is required")
    bank = _bank(body)
    # Hindsight creates banks on first write, so a typo would silently make a
    # new bank. Only write to banks that already exist.
    if bank not in core.list_banks(API_URL, API_KEY):
        raise BadRequest(f"bank {bank!r} does not exist in Hindsight")
    conn = core.connect_db(_profile(body).db)
    try:
        try:
            payload = _payload_for(conn, sid, bank)
        except BadRequest as exc:
            return {"id": sid, "ok": False, "error": str(exc)}
    finally:
        conn.close()
    with _commit_lock:
        client = core.make_client(API_URL, API_KEY)
        try:
            core.retain_new(client, payload, API_URL, API_KEY)
        except core.AlreadyInHindsight as exc:
            return {"id": sid, "ok": False, "skipped": True, "error": str(exc)}
        except Exception as exc:
            return {"id": sid, "ok": False, "error": _error_text(exc)}
        finally:
            try:
                client.close()
            except Exception:
                pass
    return {"id": sid, "ok": True, "document_id": payload["document_id"]}


def _error_text(exc: Exception) -> str:
    """Short message for a failed retain (the client's ApiException is verbose)."""
    status, body = getattr(exc, "status", None), getattr(exc, "body", None)
    if status:
        detail = body
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            pass
        return f"HTTP {status}: {str(detail)[:300]}"
    return str(exc) or type(exc).__name__


def status(query: dict) -> dict:
    """Connection status for the chosen profile's DB and bank."""
    prof = _profile(query)
    bank = (query.get("bank") or "").strip()
    return {"db": core.check_db(prof.db),
            "hindsight": core.check_hindsight(API_URL, bank or None, API_KEY),
            "config_file": str(CONFIG_PATH) if CONFIG_LOADED else None}


def profiles(_query: dict) -> dict:
    """Hermes profiles (with their default bank) and the banks Hindsight has."""
    out = {"hermes_home": str(HERMES_HOME), "url": API_URL,
           "profiles": [{"name": p.name, "db": str(p.db), "bank": p.bank,
                         "bank_source": p.bank_source, "warning": p.warning}
                        for p in core.discover_profiles(HERMES_HOME)]}
    try:
        out["banks"] = core.list_banks(API_URL, API_KEY)
    except Exception as exc:
        out["banks"], out["banks_error"] = [], str(exc)
    return out


def defaults(_query: dict) -> dict:
    d = core.FilterOptions()
    return {
        "url": API_URL,
        "min_turns": d.min_turns, "days": d.days, "min_tool_calls": d.min_tool_calls,
        "exclude_tools": sorted(d.exclude_tools),
        "exclude_rules": [vars(r) for r in core.EXCLUDE_RULES],
        "keep_rules": [vars(r) for r in core.KEEP_RULES],
    }


ROUTES_GET = {"/api/status": status, "/api/defaults": defaults, "/api/profiles": profiles}
ROUTES_POST = {"/api/sessions": list_sessions, "/api/preview": preview,
               "/api/commit": commit_one}


class Handler(BaseHTTPRequestHandler):
    server_version = "hermes-backfill-webui"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _run(self, fn, *args) -> None:
        try:
            self._json(200, fn(*args))
        except BadRequest as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:
        path, _, qs = self.path.partition("?")
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(qs).items()}
        if path in ("/", "/index.html"):
            self._send(200, (HERE / "webui.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ROUTES_GET:
            self._run(ROUTES_GET[path], query)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in ROUTES_POST:
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "body must be a JSON object"})
        self._run(ROUTES_POST[path], body)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def main() -> None:
    print(f"Hermes home:   {HERMES_HOME}")
    for p in core.discover_profiles(HERMES_HOME):
        print(f"  profile {p.name:<12} -> bank {p.bank or '(choose in UI)'}")
    print(f"Hindsight API: {API_URL}")
    print(f"Config file:   {CONFIG_PATH if CONFIG_LOADED else '(none; using environment)'}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Listening on http://{HOST}:{PORT}/  (no auth - LAN only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
