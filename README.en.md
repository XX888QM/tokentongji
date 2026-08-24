# Token Usage Dashboard

[🇨🇳 简体中文](README.md) · [🇹🇼 繁體中文](README.zh-TW.md) · 🇺🇸 **English** · [🇯🇵 日本語](README.ja.md) · [🇰🇷 한국어](README.ko.md) · [🇪🇸 Español](README.es.md)

A local desktop web dashboard that tracks token usage from **Claude Code**, **Codex**, **OpenCode**, **OpenClaw**, **Hermes**, and **Grok**. It summarizes usage by day, week, month, and all time. Logs are parsed locally with **no third-party dependencies and no external API calls except exchange rates**. Mobile layouts are not supported.

## Data sources

| Source | Path | Method |
|---|---|---|
| Claude | `~/.claude/projects/**/*.jsonl` | Reads assistant `message.usage`, deduplicates by `message.id`, and separates fallback `usage.iterations` by their real model |
| Codex | `~/.codex/sessions` + `archived_sessions`; claude-mem also reads `~/.claude-mem/usage/codex-usage-*.jsonl` | Computes adjacent deltas from cumulative `total_token_usage`; ephemeral `codex exec` calls use the exact one-shot `turn.completed.usage` value and are shown separately as `claude-mem (Codex quota)` |
| OpenCode | `~/.local/share/opencode/opencode.db` | Reads SQLite directly and syncs incrementally by message timestamp; reasoning tokens count as output |
| OpenClaw | `~/.openclaw/agents/main/sessions/*.jsonl` | Supports trajectory and v3 formats and removes identical calls duplicated across both formats |
| Hermes | `~/.hermes/state.db` | Reads cumulative session rows and synchronizes replacements; reasoning is a subset of output and is not added twice |
| Grok | `~/.grok/logs/unified.jsonl` | Reads incremental token usage from `shell.turn.inference_done` events produced by Grok CLI or claude-mem API transcripts; inline model/cwd values take priority, otherwise values carry forward by sid |

Critical Claude and Codex deduplication, delta, and fork-baseline rules have regression tests. Results still depend on each tool's log format and local history. Use the dashboard's runtime audit to check source freshness and unknown models.

> Grok statistics require an existing `unified.jsonl`. This project only reads the file; it does not install Grok or claude-mem transcript hooks. Set `TOKENSTAT_GROK_LOG` if the file is elsewhere.

## Quick start

Requires Python 3.9+ and uses only the standard library. **No `pip install` is needed.**

```bash
git clone https://github.com/XX888QM/tokentongji.git
cd tokentongji

# 1) Recommended: run the initial full ingest (duration depends on log history)
PYTHONPATH=src python3 -m tokenstat.ingest

# 2) Start the web service and the 60-second background ingest loop
PYTHONPATH=src python3 -m tokenstat.server

# 3) Open the dashboard
open http://127.0.0.1:8787
```

## Dashboard features

- Today, last 7 days, this month, and all-time token totals with estimated CNY cost and per-source shares; Codex is split into direct Codex and `claude-mem (Codex quota)`
- Chinese large-number units (`万 / 亿 / 万亿 / 京 / 垓`) with exact values on hover
- 30-day multi-source token trend chart, including a separate claude-mem series
- Model and project breakdowns with totals, cache-token columns, costs, period switching, and a `claude-mem · Codex` marker where applicable
- Runtime audit for source paths, ingest progress, unknown models, and mixed-source sessions
- Anomaly insights for the largest model/project contributions and baseline comparisons
- Top 10 most expensive sessions with model and source-file details
- Automatic refresh every 30 seconds

Costs are displayed in CNY. The page immediately uses a cached exchange rate (7.25 on first launch), while the server refreshes USD→CNY from `open.er-api.com` in the background and caches it for one hour. Network failures do not block the dashboard.

### claude-mem accounting

claude-mem uses Codex quota; it is not an additional Codex charge. The dashboard separates physical Codex data into two **display sources**: `Codex (direct)` and `claude-mem (Codex quota)`. They always add up to physical Codex usage, without double-counting totals or costs. Source share, period cards, trend, details, sessions, and CSV use the same split; runtime audit still checks physical Codex.

## Startup

On this Mac the dashboard is a LaunchAgent (`com.yunxin.tokenstat`). launchd cannot read `~/Desktop`, so `scripts/install-launchd.sh` copies code and the DB to `~/Library/Application Support/tokenstat/` and points the plist at that copy.

```bash
bash scripts/install-launchd.sh
# → http://127.0.0.1:8787
```

Re-run the install script after changing the repo. Logs: `~/Library/Logs/tokenstat/`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TOKENSTAT_HOST` | 127.0.0.1 | Listen address |
| `TOKENSTAT_PORT` | 8787 | Web port; must be a positive integer |
| `TOKENSTAT_INGEST_INTERVAL` | 60 | Background ingest interval in seconds; must be positive |
| `TOKENSTAT_REFRESH` | 30 | Dashboard refresh interval in seconds; must be positive |
| `TOKENSTAT_STALE_DAYS` | 3 | Warn after a source has no new data, or trails other sources, by this many days |
| `TOKENSTAT_DATA_DIR` | Application Support copy after install, else `./data` | SQLite and backup directory |
| `TOKENSTAT_GROK_LOG` | `~/.grok/logs/unified.jsonl` | Grok unified log path |
| `TOKENSTAT_CLAUDE_MEM_CODEX_USAGE_DIR` | `~/.claude-mem/usage` | Directory containing claude-mem Codex one-shot usage JSONL files |

Pricing is configured in `src/tokenstat/pricing.json` in USD per million tokens. Local and self-hosted models use the zero-rate `local` section. `codex-auto-review` is estimated using the public OpenAI Codex `gpt-5.3-codex` price; `gpt-5-codex` uses its own public price.

**Note:** Under Claude Max, Codex, or Grok subscriptions, token usage does not directly equal a charge. All costs are estimates for reference only.

## Tests

Node.js is required only for frontend amount-format regression tests. Running the dashboard itself only requires Python.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Troubleshooting

- Empty dashboard shell: open `http://127.0.0.1:8787/api/health`. If it does not respond, the service is stopped or the port is occupied.
- Missing source: verify the corresponding path above and check Runtime Audit. A missing source does not prevent other sources from loading.
- Address already in use: stop the existing manual service, or set a different `TOKENSTAT_PORT`.

## Architecture

```text
src/tokenstat/
  config.py      Global paths, ports, and intervals
  models.py      Normalized UsageRecord model
  db.py          SQLite deduplication and ingest checkpoints
  parsers/
    claude.py    Claude message-id deduplication and fallback iterations
    codex.py     Codex cumulative-total deltas and context carry-forward
    opencode.py  Direct incremental OpenCode SQLite reader
    openclaw.py  OpenClaw trajectory and v3 formats
    hermes.py    Hermes SQLite sessions with full-table replacement sync
    grok.py      Grok inference_done events and sid carry-forward
  ingest.py      Incremental byte-offset ingestion
  pricing.py     Cost estimates and model normalization
  pricing.json   anthropic / openai / deepseek / xai / local rates
  aggregate.py   Daily, weekly, monthly, and all-time queries
  server.py      HTTP API, static files, exchange rates, and ingest thread
  static/        index.html / app.js / styles.css / chart.min.js
```

`docs/superpowers/` contains dated design and implementation records, not the current user guide. Current behavior is defined by the main README, `CLAUDE.md`, code, and tests.
