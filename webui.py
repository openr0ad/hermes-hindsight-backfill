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

DB_PATH = core.DEFAULT_DB
API_URL = core.DEFAULT_API_URL
BANK_ID = core.DEFAULT_BANK_ID
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

    tools = body.get("exclude_tools")
    try:
        return core.FilterOptions(
            min_turns=num("min_turns", 6),
            days=num("days", 90),
            until=until,
            exclude_missing_session_key=bool(body.get("exclude_missing_session_key")),
            min_tool_calls=num("min_tool_calls", 1),
            extra_exclude_patterns=str_list("extra_exclude_patterns"),
            extra_keep_patterns=str_list("extra_keep_patterns"),
            exclude_tools=set(str_list("exclude_tools")) if tools is not None
            else set(core.DEFAULT_EXCLUDE_TOOLS),
        )
    except re.error as exc:
        raise BadRequest(f"invalid regex: {exc}")


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def list_sessions(body: dict) -> dict:
    opts = _filter_options(body)
    conn = core.connect_db(DB_PATH)
    try:
        # min_turns=0: list short sessions too, with a reason, so they can be
        # selected manually. evaluate_session() applies the real threshold.
        sessions = core.get_db_sessions(conn, 0, opts.days, opts.until, False)
        out = []
        for s in sessions:
            v = core.evaluate_session(conn, s, opts)
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
            })
    finally:
        conn.close()
    return {"sessions": out, "count": len(out),
            "included": sum(1 for s in out if s["included"])}


def _payload_for(conn, session_id: str) -> dict:
    """Payload for one session, independent of filters (manual selection wins)."""
    s = core.get_session(conn, session_id)
    if not s:
        raise BadRequest(f"unknown session {session_id}")
    msgs = core.get_session_messages(conn, session_id)
    if not msgs:
        raise BadRequest("session has no messages; nothing to send")
    return core.build_retain_payload(s, core.format_conversation(msgs, s), BANK_ID)


def _ids(body: dict) -> list[str]:
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise BadRequest("ids must be a list of session ids")
    return ids


def preview(body: dict) -> dict:
    """Dry run: build exactly what would be sent. Makes no Hindsight calls."""
    conn = core.connect_db(DB_PATH)
    items = []
    try:
        for sid in _ids(body):
            try:
                items.append({"id": sid, "ok": True, "payload": _payload_for(conn, sid)})
            except BadRequest as exc:
                items.append({"id": sid, "ok": False, "error": str(exc)})
    finally:
        conn.close()
    return {"url": API_URL, "items": items}


def commit_one(body: dict) -> dict:
    sid = body.get("id")
    if not isinstance(sid, str):
        raise BadRequest("id is required")
    conn = core.connect_db(DB_PATH)
    try:
        try:
            payload = _payload_for(conn, sid)
        except BadRequest as exc:
            return {"id": sid, "ok": False, "error": str(exc)}
    finally:
        conn.close()
    with _commit_lock:
        client = core.make_client(API_URL, API_KEY)
        try:
            core.retain_payload(client, payload)
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


def duplicates(body: dict) -> dict:
    ids = _ids(body)
    idx = core.fetch_bank_session_index(API_URL, BANK_ID, API_KEY)
    statuses = {sid: st for sid in ids if (st := core.duplicate_status(sid, idx))}
    return {"documents_scanned": idx.documents_scanned, "statuses": statuses,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def status() -> dict:
    return {"db": core.check_db(DB_PATH),
            "hindsight": core.check_hindsight(API_URL, BANK_ID, API_KEY),
            "config_file": str(CONFIG_PATH) if CONFIG_LOADED else None}


def defaults() -> dict:
    d = core.FilterOptions()
    return {
        "bank": BANK_ID, "url": API_URL, "db": str(DB_PATH),
        "min_turns": d.min_turns, "days": d.days, "min_tool_calls": d.min_tool_calls,
        "exclude_tools": sorted(d.exclude_tools),
        "default_exclude_patterns": core.DEFAULT_EXCLUDE_PATTERNS,
        "default_keep_patterns": core.DEFAULT_KEEP_PATTERNS,
    }


ROUTES_GET = {"/api/status": status, "/api/defaults": defaults}
ROUTES_POST = {"/api/sessions": list_sessions, "/api/preview": preview,
               "/api/commit": commit_one, "/api/duplicates": duplicates}


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
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, (HERE / "webui.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ROUTES_GET:
            self._run(ROUTES_GET[path])
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
    print(f"Hermes DB:     {DB_PATH}")
    print(f"Hindsight API: {API_URL} (bank {BANK_ID!r})")
    print(f"Config file:   {CONFIG_PATH if CONFIG_LOADED else '(none; using environment)'}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Listening on http://{HOST}:{PORT}/  (no auth — LAN only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
