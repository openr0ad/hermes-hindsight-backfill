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

Sessions pass through a four-stage filter before being retained:

1. **Date + turn filter** — must be within `--days` window and have at least
   `--min-turns` exchanges
2. **Exclusion patterns** — sessions whose title matches any pattern in
   `DEFAULT_EXCLUDE_PATTERNS` (or `--exclude-pattern`) are skipped
3. **Substantive check** — must have at least one tool call or a code block;
   pure chat sessions are skipped
4. **Length check** — formatted text must be at least 200 characters

A state file (`~/.hermes/.backfill_state.json`) tracks retained session IDs
so re-runs skip already-processed sessions automatically. Pass
`--no-skip-tagged` to reprocess everything.

What gets retained: technical sessions with real back-and-forth — debugging,
config changes, code, automation work, etc.

## Prerequisites

- [Hermes](https://github.com/nousresearch/hermes) installed with session
  history in `~/.hermes/state.db`
- [Hindsight](https://github.com/vectorize-io/hindsight) running and accessible
  (default: `http://localhost:8888`)
- `hindsight-all` Python package:

```bash
uv pip install hindsight-all
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
# Dry run — preview what would be retained (no writes)
python3 backfill_hindsight.py --dry-run

# Inspect session text before committing
python3 backfill_hindsight.py --dry-run --show-text --limit 3

# Real run: last 90 days, min 6 turns
python3 backfill_hindsight.py --days 90 --min-turns 6

# Custom Hindsight server and bank
python3 backfill_hindsight.py --url http://my-server:8888 --bank-id my-bank

# Exclude sessions by title pattern (repeatable)
python3 backfill_hindsight.py --days 90 \
  --exclude-pattern "\bweather\b" \
  --exclude-pattern "\bdinner\b"

# Filter output to only retained sessions
python3 backfill_hindsight.py --days 90 | grep -E " retained|Done."

# Reprocess everything, ignoring the state file
python3 backfill_hindsight.py --no-skip-tagged
```

## Options

| Flag                | Default                          | Description                                                    |
| ------------------- | -------------------------------- | -------------------------------------------------------------- |
| `--db`              | `~/.hermes/state.db`             | Path to Hermes session database                                |
| `--url`             | `http://localhost:8888`          | Hindsight API base URL                                         |
| `--bank-id`         | `hermes`                         | Hindsight memory bank ID                                       |
| `--min-turns`       | `6`                              | Minimum exchanges (user+assistant pairs) per session           |
| `--days`            | `90`                             | How far back to look                                           |
| `--limit`           | `0` (all)                        | Cap sessions processed                                         |
| `--dry-run`         | off                              | Preview without writing to Hindsight                           |
| `--show-text`       | off                              | Print formatted text preview (dry-run only)                    |
| `--no-skip-tagged`  | off                              | Reprocess already-retained sessions                            |
| `--state-file`      | `~/.hermes/.backfill_state.json` | Custom path for state ledger                                   |
| `--min-tool-calls`  | `1`                              | Tool call threshold for "substantive"                          |
| `--exclude-pattern` | —                                | Regex to skip sessions by title (repeatable, case-insensitive) |

## Tuning exclusion patterns

The script ships with default exclusions for high-volume, low-value titles:

```
quick question, weather, breakfast, lunch, dinner,
macro(s), logged, nutrition, greeting
```

Exclusion matches only on session **title**, not message content. A session
that starts as a food log but pivots into a technical discussion will have a
food-themed title and can get excluded. Review borderline sessions with
`--show-text` before committing.

Add patterns permanently in `DEFAULT_EXCLUDE_PATTERNS` at the top of the script,
or pass them at runtime via `--exclude-pattern`.

## Finding your oldest session

```bash
sqlite3 ~/.hermes/state.db \
  "SELECT title, datetime(started_at, 'unixepoch', 'localtime') \
   FROM sessions WHERE started_at > 1700000000 ORDER BY started_at ASC LIMIT 1;"
```

Use the resulting date to set `--days` appropriately for a full backfill.

## Requirements

- Python 3.9+

## License

MIT
