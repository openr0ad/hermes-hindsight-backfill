# Hermes Hindsight Backfill

Backfill historical [Hermes](https://github.com/nousresearch/hermes) sessions into [Hindsight](https://github.com/vectorize-io/hindsight) long-term memory.

Hindsight only captures sessions going forward from when it was set up. This script bridges that gap by reading existing sessions from Hermes' local SQLite database, filtering for quality, and pushing them into Hindsight via the retain API.

```
┌─────────────┐    ┌──────────────┐    ┌────────────┐    ┌──────────────┐
│ Hermes      │───▶│ Filter:      │───▶│ Format as  │───▶│ Hindsight    │
│ state.db    │    │ substantive  │    │ plain text │    │ retain() API │
│ (SQLite)    │    │ sessions     │    │            │    │              │
└─────────────┘    └──────────────┘    └────────────┘    └──────────────┘
```

## Filter pipeline

Sessions pass through a five-stage filter before being retained:

1. **Date + turn filter** — must be within `--days` window (and on/before
   `--until` if given) and have at least `--min-turns` exchanges
2. **Title rules** — sessions whose title mentions a skip word (`EXCLUDE_RULES`,
   or `--exclude-pattern`) are skipped, unless it also mentions a keep word
3. **Excluded tools** — sessions that called any tool in `DEFAULT_EXCLUDE_TOOLS`
   (or `--exclude-tool`) are skipped — see below
4. **Substantive check** — must have at least one tool call or a code block;
   pure chat sessions are skipped
5. **Length check** — formatted text must be at least 200 characters

A state file (`~/.hermes/.backfill_state.json`) tracks retained session IDs
so CLI re-runs skip already-processed sessions automatically. Pass
`--no-skip-tagged` to reprocess everything. (The web UI doesn't use this
file; it asks Hindsight directly — see [Duplicate check](#duplicate-check).)

## Session identity in Hindsight

Each backfilled session is retained with:

| Field         | Value                                   |
| ------------- | --------------------------------------- |
| `document_id` | `<hermes session id>`                   |
| `tags`        | `backfill`, `session:<hermes session id>` |
| `metadata`    | `session_id`, `source=hermes-hindsight-backfill` |

This is the same identity the Hindsight Hermes plugin uses for live sessions
(document id = session id, `session:<id>` tag, `session_id` metadata), so a
session has **one document** no matter which path stored it. The `backfill`
tag records where it came from.

**Sessions already in Hindsight are never sent.** Retaining with an existing
document id would *replace* that document (for a live-captured session, the
plugin's), so before each send the tool looks the session up by document id
and by `session:<id>` tag, and refuses if anything is found. This applies to
both the CLI and the web UI, and to sessions ticked by hand. To re-send a
session, delete its document in Hindsight first.

> **Older backfills** (before this change) were tagged only
> `session:<first 8 chars>`, which for Hermes ids is just the date
> (`YYYYMMDD`). They have random document ids and can't be tied to one
> session; the duplicate check reports them as *possible* duplicates for every
> session started that day.

### Sessions with a NULL `session_key`

By default sessions are included **even if `session_key` is NULL**. That column
is only populated for sessions that ended cleanly, so excluding NULLs silently
drops a large share of otherwise-good sessions — anything cut short by a crash,
power loss, or gateway restart. On a real database that was ~79% of all rows.
Pass `--exclude-missing-session-key` for the old, stricter behaviour (only
sessions with a populated key).

What gets retained: technical sessions with real back-and-forth — debugging,
config changes, code, automation work, etc.

### Excluding meal logs (and other write-only sessions)

Title regexes are a blunt tool: they cannot tell *"Log meals for today"* (a
write-only entry you don't want in memory) from *"Review peanut logs after
server closet outage"* (real technical work). Matching on the tools a session
actually called is precise instead. `log_meal` — the healthlog meal tracker —
is excluded by default, so meal-logging sessions never reach Hindsight:

```bash
# add another tool to the exclusion set (repeatable)
python3 backfill_hindsight.py --days 90 --exclude-tool log_peptide

# keep everything, including meal logs
python3 backfill_hindsight.py --days 90 --no-exclude-tools
```

The meal words in the title rules remain as a fallback for older
sessions that logged meals without going through the plugin.

**Engineering work wins.** Any title mentioning a keep word (`fix`,
`rebuild`, `debug`, `prompt`, `protocol`, `integration`, `.csv`, `error`,
`script`, `bug`, `skill`, `schema`, `cron`) is kept regardless of both filters —
so *"Rebuilding meals.csv after write error"* and *"New nutrition logging
integration protocol"* are retained as the technical sessions they are, while
*"Log meals for today"* is dropped. Extend with `--keep-pattern`.

A session that logs a meal *and* does technical work is skipped unless its title
matches a keep pattern — `--no-exclude-tools` is the broader escape hatch.

## Prerequisites

- [Hermes](https://github.com/nousresearch/hermes) installed with session
  history in `~/.hermes/state.db`
- [Hindsight](https://github.com/vectorize-io/hindsight) running and accessible
  (default: `http://localhost:8888`). Use the **API** port, not the Control
  Plane UI port.
- `hindsight-client` Python package (installed automatically by `uv sync` /
  `uv run`; the full `hindsight-all` stack is not needed):

```bash
uv pip install hindsight-client
```

## Installation

```bash
# Install as a tool (recommended)
uv tool install hermes-hindsight-backfill

# Or pip
pip install hermes-hindsight-backfill

# Or just clone and run directly
uv run backfill_hindsight.py
```

## Usage

```bash
# Dry run — preview what would be retained (no writes, no Hindsight calls)
python3 backfill_hindsight.py --dry-run

# Inspect session text before committing
python3 backfill_hindsight.py --dry-run --show-text --limit 3

# Real run: last 90 days, min 6 turns
python3 backfill_hindsight.py --days 90 --min-turns 6

# Target a specific window (e.g. an ingestion gap): Oct 6 through Oct 31
python3 backfill_hindsight.py --days 31 --until 2026-10-31 --min-turns 6

# Custom Hindsight server and bank
python3 backfill_hindsight.py --url http://my-server:8888 --bank-id my-bank

# Hindsight on another host (Docker/VM) — set once instead of passing --url
export HINDSIGHT_API_URL=http://<hindsight-host>:8888
python3 backfill_hindsight.py --days 90

# Exclude sessions by title pattern (repeatable)
python3 backfill_hindsight.py --days 90 \
  --exclude-pattern "\bweather\b" \
  --exclude-pattern "\bdinner\b"

# Filter output to only retained sessions
python3 backfill_hindsight.py --days 90 | grep -E " retained|Done."

# Reprocess everything, ignoring the state file
python3 backfill_hindsight.py --no-skip-tagged
```

## Hermes profiles and banks

Each Hermes profile keeps its own sessions and can send its memories to its
own Hindsight bank:

| Profile        | Sessions database                       | Bank comes from                                  |
| -------------- | --------------------------------------- | ------------------------------------------------ |
| `default`      | `~/.hermes/state.db`                    | `~/.hermes/hindsight/config.json`                |
| `<name>`       | `~/.hermes/profiles/<name>/state.db`    | `~/.hermes/profiles/<name>/hindsight/config.json` |

The bank is picked the same way the Hindsight Hermes plugin picks it, so
backfilled sessions land where that profile's live sessions go:

- `bank_id` from the profile's `hindsight/config.json`, or
- `bank_id_template`, if it only uses `{profile}` (e.g. `hermes-{profile}`), or
- the shared `~/.hindsight/config.json`, if the profile has no config of its own.

If none of these applies, or the template depends on the session (e.g.
`{project}`), the tool can't tell which bank to use. The CLI then requires
`--bank-id`, and the web UI asks you to choose. Several profiles may share a
bank. Backups under `state-snapshots/` are ignored.

```bash
python3 backfill_hindsight.py --list-profiles
python3 backfill_hindsight.py --profile ops --dry-run
python3 backfill_hindsight.py --profile coder --bank-id hermes --dry-run   # no bank configured
```

Sessions are only ever sent to banks that already exist in Hindsight.
Hindsight would otherwise create a new bank on the first write, so a typo
would quietly start a new, empty memory.

## Options

| Flag                | Default                          | Description                                                    |
| ------------------- | -------------------------------- | -------------------------------------------------------------- |
| `--profile`         | —                                | Hermes profile: use its session DB and its Hindsight bank (see [Profiles](#hermes-profiles-and-banks)) |
| `--list-profiles`   | —                                | List profiles with their DB and bank, then exit                |
| `--hermes-home`     | `$HERMES_HOME` → `~/.hermes`     | Where Hermes keeps `state.db` and `profiles/`                  |
| `--db`              | profile's, else `$HERMES_DB` → `~/.hermes/state.db` | Path to Hermes session database (opened read-only) |
| `--url`             | `$HINDSIGHT_API_URL` → `http://localhost:8888` | Hindsight API base URL (Hindsight is often on another host) |
| `--bank-id`         | profile's, else `$HINDSIGHT_BANK` → `hermes` | Hindsight memory bank ID                           |
| `--min-turns`       | `6`                              | Minimum exchanges (user+assistant pairs) per session           |
| `--days`            | `90`                             | How far back to look                                           |
| `--until`           | none                             | Upper bound: only sessions started on/before this date (`YYYY-MM-DD` = end of day, inclusive) |
| `--limit`           | `0` (all)                        | Cap sessions processed                                         |
| `--dry-run`         | off                              | Preview without writing to Hindsight                           |
| `--show-text`       | off                              | Print formatted text preview (dry-run only)                    |
| `--no-skip-tagged`  | off                              | Reprocess already-retained sessions                             |
| `--exclude-missing-session-key` | off                  | Only include sessions with a populated `session_key` (see note) |
| `--state-file`      | `~/.hermes/.backfill_state.json` | Custom path for state ledger                                   |
| `--min-tool-calls`  | `1`                              | Tool call threshold for "substantive"                          |
| `--exclude-pattern` | —                                | Regex to skip sessions by title (repeatable, case-insensitive) |
| `--keep-pattern`    | —                                | Regex that forces a title to be kept, overriding both exclusions |
| `--exclude-tool`    | `log_meal`                       | Skip sessions that called this tool (repeatable)               |
| `--no-exclude-tools`| off                              | Disable tool-based exclusion entirely                          |
| `--disable-rule`    | —                                | Switch off a built-in title rule by id (repeatable)            |
| `--list-rules`      | —                                | List built-in title rules with their ids, then exit            |

## Web UI

A small local web UI (`webui.py` + `webui.html`) wraps the same filter and
retain code as the CLI. It's a single Python process on the standard-library
HTTP server, with no extra dependencies. It's meant to run on the same machine
as Hermes (e.g. a Raspberry Pi) and be opened from any browser on your LAN.

What it does:

- **Profile and bank**: pick a Hermes profile. Its sessions load, and its
  bank is selected (see [Profiles](#hermes-profiles-and-banks)). You can send
  to a different existing bank; the UI notes when that differs from the
  profile's own.
- **Connection status**: whether the profile's Hermes DB is found and
  readable, and whether the Hindsight API and bank are reachable.
- **Session list**: every session in the date window, with *all* the reasons
  the filters exclude it (too few turns, title pattern, excluded tool, chat
  only, too short) plus keep-pattern overrides.
- **Selection**: tick/untick rows, select all / none, reset to the filters.
  A manual selection overrides the filters.
- **Title rules** in plain language ("Skip sessions whose title mentions
  *breakfast*"), each with its own checkbox, plus your own words or phrases.
  An advanced option takes regular expressions instead.
- **Preview** (selected rows, or **View** on a single row): shows exactly what
  would be sent — document id, tags, metadata and the full text. Nothing is sent.
- **Send selected to Hindsight**: retains the selected sessions one at a time
  and shows the result for each. Sessions Hindsight already has are refused
  ("not sent: already there") — see [Session identity](#session-identity-in-hindsight).
- <a id="duplicate-check"></a>**Check what's already in Hindsight** (on demand): lists the
  bank's documents (read-only) and marks sessions that are already present,
  matched on the full session id from either the live Hermes plugin or this
  tool. Exact duplicates are deselected. Old date-only backfills show as
  *possible* duplicates (see [Session identity](#session-identity-in-hindsight)).

> ⚠️ There is **no authentication**. By default it listens on `0.0.0.0`, so
> anyone on your LAN can open it and commit to Hindsight. Set
> `WEBUI_HOST=127.0.0.1` to restrict it to the local machine.

### Configuration

Settings come from environment variables. Optionally, put them in a
`config.env` file next to `webui.py` (git-ignored), or in a file named by
`$WEBUI_CONFIG`. Real environment variables take precedence over the file.

```bash
cp config.example.env config.env   # then edit
```

| Variable            | Default                 | Description                                   |
| ------------------- | ----------------------- | --------------------------------------------- |
| `HERMES_HOME`       | `~/.hermes`             | Hermes home with `state.db` and `profiles/` (relative paths are relative to the working directory) |
| `HINDSIGHT_API_URL` | `http://localhost:8888` | Hindsight **API** URL                         |
| `HINDSIGHT_API_KEY` | —                       | Bearer token, if your API requires one        |
| `WEBUI_HOST`        | `0.0.0.0`               | Bind address                                  |
| `WEBUI_PORT`        | `8765`                  | Port                                          |

The CLI reads `HERMES_HOME`, `HERMES_DB`, `HINDSIGHT_API_URL` and
`HINDSIGHT_BANK` from the environment too. It does not read `config.env`.

### Running on a Raspberry Pi

Requires Python 3.11+. Raspberry Pi OS **Bookworm** ships 3.11; **Bullseye**
ships 3.9, which is too old. uv handles both: it downloads its own Python for
this project and leaves the system Python alone.

```bash
git clone https://github.com/openr0ad/hermes-hindsight-backfill.git
cd hermes-hindsight-backfill

# Recommended: uv (fetches Python 3.11+ if the system's is older)
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync            # creates .venv with the right Python and dependencies

# Alternative, only if `python3 --version` is 3.11 or newer:
#   python3 -m venv .venv && .venv/bin/pip install hindsight-client

cp config.example.env config.env
nano config.env      # set HINDSIGHT_API_URL=http://<hindsight-host>:8888

.venv/bin/python webui.py
# → open http://<pi-hostname>:8765/ from any machine on the LAN
```

To keep it running across reboots, create a systemd service. Replace `<you>`
with your Pi username:

```ini
# /etc/systemd/system/hermes-backfill-webui.service
[Unit]
Description=Hermes → Hindsight backfill web UI
After=network-online.target
Wants=network-online.target

[Service]
User=<you>
WorkingDirectory=/home/<you>/hermes-hindsight-backfill
ExecStart=/home/<you>/hermes-hindsight-backfill/.venv/bin/python webui.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-backfill-webui
journalctl -u hermes-backfill-webui -f    # logs
```

### Development (Windows or any other machine)

Copy the Pi's Hermes layout into `dev-data/` (git-ignored), mirroring
`~/.hermes`:

```
dev-data/
  state.db, state.db-wal, state.db-shm      # default profile
  hindsight/config.json                     # default profile's bank
  profiles/<name>/state.db (+ -wal, -shm)
  profiles/<name>/hindsight/config.json
```

Copy each `state.db` together with its `-wal` and `-shm` files. The
`hindsight/config.json` files may contain an API key; `dev-data/` is never
committed. Then point the config at it:

```ini
# config.env
HERMES_HOME=./dev-data
HINDSIGHT_API_URL=http://<hindsight-host>:8888
WEBUI_HOST=127.0.0.1
```

```bash
uv run webui.py
```

Databases are always opened read-only.

## Tuning title rules

The script ships with built-in title rules. Each has an id and a plain label:

```bash
python3 backfill_hindsight.py --list-rules
```

```
Skip sessions whose title mentions:
  quick-question   "quick question"
  weather          "weather"
  breakfast        "breakfast"
  ...
Always keep sessions whose title mentions (overrides skips):
  fix              "fix"
  ...
```

Switch individual rules off with `--disable-rule` (repeatable), or add your own
regexes with `--exclude-pattern` / `--keep-pattern`:

```bash
python3 backfill_hindsight.py --dry-run --disable-rule breakfast --disable-rule lunch
python3 backfill_hindsight.py --dry-run --exclude-pattern "\bgrocer(y|ies)\b"
```

In the web UI the same rules are checkboxes under **Title rules**, and custom
entries are plain words or phrases (matched as whole words, ignoring case).

Rules match only on the session **title**, not message content. A session
that starts as a food log but pivots into a technical discussion will have a
food-themed title and can get excluded. Review borderline sessions with
`--show-text` (or **View** in the web UI) before sending.

To change the built-ins permanently, edit `EXCLUDE_RULES` / `KEEP_RULES` at the
top of the script.

## Finding your oldest session

```bash
sqlite3 ~/.hermes/state.db \
  "SELECT title, datetime(started_at, 'unixepoch', 'localtime') \
   FROM sessions WHERE started_at > 1700000000 ORDER BY started_at ASC LIMIT 1;"
```

Use the resulting date to set `--days` appropriately for a full backfill.

## Requirements

- Python 3.11+

## License

MIT
