# Tranquil

Tranquil is a local-first observability and eval layer for coding agents. It
runs a localhost collector, stores normalized events in SQLite, serves a fleet
dashboard, detects unhealthy runs, and can promote captured runs into eval
fixtures.

This repository is dependency-light. `pip install .` gives you a working
`tranquil` command without a separate service. The default command runs the
local collector and a Rich terminal Fleet view in the same terminal; the web
dashboard remains available from that same process.

## Quick start

```bash
python -m pip install -e .
tranquil init
```

In an interactive terminal, `tranquil init` wires hooks and launches the local
terminal app. Use `tranquil init --no-launch` when you only want to update
config, or run `tranquil` later to start the collector plus terminal UI. Open
the printed URL only when you want the secondary web dashboard. Hook events can
be sent to:

```text
POST http://127.0.0.1:8787/hooks/post-tool-use
Authorization: Bearer <token from ~/.tranquil/config.json>
```

The collector acknowledges hook requests quickly and persists on a background
worker. If the process is down, agents continue normally; transcript and Codex
rollout tailing provide the durable fallback capture path.

To enable the dashboard's Open action for touched files, set
`editor_command` in `~/.tranquil/config.json`, for example
`code -g {path}:{line}`. `TRANQUIL_EDITOR_COMMAND` can also provide the same
template.

To enable the `outcome_judge` scorer, set `judge_command` in
`~/.tranquil/config.json`, pass `--judge-command`, or set
`TRANQUIL_JUDGE_COMMAND`. The command receives a JSON object on stdin with the
fixture prompt, rubric, reference, repo metadata, run summary, and events. It
must print a JSON object such as:

```json
{"passed": true, "score": 0.9, "reason": "rubric satisfied"}
```

Signals can also be delivered outside the dashboard by setting
`notification_webhook_url` or `notification_command` in
`~/.tranquil/config.json`. `TRANQUIL_NOTIFICATION_WEBHOOK_URL` and
`TRANQUIL_NOTIFICATION_COMMAND` provide the same settings from the environment.
Both receive a JSON payload with `type: "signal"` and the signal record.

Optional pre-tool guardrails can deny risky tool calls before they run. Set
`policy_enabled: true` plus `policy_forbidden_paths` or
`policy_forbidden_commands` in `~/.tranquil/config.json`. Matching `PreToolUse`
events return a deny decision and create a `policy_denied` signal.

To continuously grow regression coverage from real runs, set
`trace_sampling_enabled: true`, `trace_sample_rate`, and optionally
`trace_sample_suite` in `~/.tranquil/config.json`. Completed runs whose stable
sample bucket is under the configured rate are saved as fixtures. You can also
run `tranquil fixture sample` manually to backfill from already captured runs.

Team/cloud sync is push-only and opt-in. Set `sync_endpoint` in config or pass
`tranquil sync --endpoint ...`; Tranquil posts a local export to that endpoint
only when you run the command.

## Commands

```text
tranquil                       Start the collector and Rich terminal Fleet view
tranquil app                   Same as above
tranquil serve                 Start collector and web dashboard only
tranquil init                  Wire local hooks and launch the terminal app
tranquil init --no-launch      Wire local hooks without starting Tranquil
tranquil init --undo           Remove Tranquil-managed hook entries
tranquil doctor                Check config, SQLite, collector health, and flow
tranquil doctor --no-live      Skip the synthetic collector event check
tranquil doctor --codex-audit  Inspect configured Codex rollout paths and report
                               local coverage fields
tranquil status                Print fleet status
tranquil status --line         Print compact status-line output
tranquil status --agent codex --repo api --branch main --label task=auth --table
                               Filter the terminal fleet table
tranquil signals               List active signals
tranquil stop <run_id>         Request local stop; future pre-tool hooks for
                               that run are denied
tranquil ingest <path>         Backfill Claude JSONL or Codex SQLite data
tranquil fixture add <run_id>  Promote a captured run to an eval fixture
  --cost-budget 1.50 --latency-budget 900 --forbid ".env" --forbid "secrets/**"
tranquil fixture import tranquil/fixtures/refactor-auth.yaml
                               Import an appendix-style fixture definition
tranquil fixture derive        Promote active signaled runs to fixtures
tranquil fixture sample --suite sampled --rate 0.10
                               Sample completed production traces into fixtures
tranquil eval [suite] --baseline last-green
                               Score fixtures and fail on regressions
tranquil eval [suite] --scorer tests_pass --scorer no_loops
                               Run only selected deterministic scorers
tranquil eval [suite] --scorer outcome_judge --judge-command "..."
                               Run a command-backed subjective outcome judge
tranquil eval tranquil/suites/refactor.yaml
                               Import suite fixtures, run scorers, and replay
                               matrix variants when configured
tranquil replay <fixture_id> --command "..." 
                               Run an explicit replay command in an isolated dir
tranquil replay <fixture_id> --agent codex --model gpt-5.5
                               Replay with codex exec in a materialized worktree
tranquil mcp                   Run the local MCP stdio server
tranquil export --json out.json
                               Export local events, runs, fixtures, scores
tranquil export --otel http://localhost:4318/v1/logs
                               Export events as OTLP/HTTP JSON logs
tranquil sync --endpoint https://example.internal/tranquil
                               Push a local export to an opt-in sync endpoint
tranquil purge --older-than 30 Delete older local runs and related data
tranquil tui                   Rich terminal Fleet view with live refresh,
                               keyboard navigation, peek, and stop request
tranquil tui --run <run_id>    Terminal Run view with signals, files, scores,
                               subagents, and recent events
```

## Scope

Implemented now:

- Canonical event normalization for Claude Code and Codex-shaped payloads.
- Localhost HTTP hook collector with bearer-token validation.
- Claude Code HTTP hook wiring and Codex command-hook wiring via a packaged
  fail-open forwarder.
- Redaction by default plus `raw_payloads: false` support for dropping vendor
  raw payloads before persistence.
- JSONL transcript backfill and background JSONL tailing for configured paths.
- Generic Codex SQLite rollout import and background rollout tailing for
  JSON-like rows.
- Local Codex rollout audit reports for discovered tables, event hints, and
  coverage fields.
- Cross-source hook/transcript reconciliation with hook payloads preferred for
  richer telemetry.
- SQLite store with events, runs, fixtures, eval runs, scores, and signals.
- Fleet dashboard with state/vendor/repo/branch/label filters, run detail view
  with subagent summaries, file read/write counts, eval score chips, touched-file
  Open actions, inline edit/write diff previews, eval summary panel, replay
  action, and run-diff action served by the local process, with REST APIs and
  live WebSocket refresh.
- Signals for loops, runaway cost, skipped checks, reread thrash, idle runs,
  scheduled/background idle runs, and failure cascades, with optional webhook or
  command notifications.
- Manual stop requests through `tranquil stop <run_id>` and the TUI, enforced by
  denying future `PreToolUse` hooks for that run.
- Optional `PreToolUse` policy guardrails for forbidden paths and command
  patterns.
- Idempotent Claude Code hook and MCP-server install/undo with a managed
  metadata block.
- Basic fixture and eval flow with `tests_pass`, `build_succeeds`,
  `diff_applies`, `no_loops`, and command-backed `outcome_judge` scorers.
- Fixture budgets, forbidden-path scorers, last-green regression checks,
  suite-matrix replay variants, and git-worktree replay materialization,
  including tracked dirty patches captured with fixtures.
- Production-trace sampling into replayable fixtures, manually through
  `tranquil fixture sample` or continuously through opt-in server config.
- Appendix-style fixture and suite YAML definitions, including rubric/reference
  metadata for judge scorers, without adding a runtime YAML dependency.
- MCP stdio server exposing run query with `since` filters, run detail, run
  diff, cost rollups with `since`/`window`, signal, and eval-status tools.
- Local JSON export, OTLP/HTTP log export, opt-in sync push, and purge controls.

Codex currently runs command hooks, so `tranquil init --agent codex` writes a
`hooks.json` entry that runs `tranquil hook-forward`. The forwarder reads the
hook JSON from stdin and posts it to the same local collector as Claude Code.
Codex may require reviewing the new hook with `/hooks` before it runs it.
