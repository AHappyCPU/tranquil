# Tranquil

Tranquil is a local-first, terminal-native observability and eval layer for
coding agents. It captures agent activity through command hooks that write
directly to a local SQLite database, renders local fleet status, detects
unhealthy runs, and can promote captured runs into eval fixtures.

The rewrite is moving Tranquil to Rust. The Rust binary is now the primary path
for config creation, command-hook ingestion, SQLite persistence, status,
signals, stop requests, policy denial, transcript and Codex rollout backfill,
tail/app rendering, notifications, fixtures, suite import, deterministic evals
with baselines, matrix replay, outcome judging, trace sampling, git-worktree
replay, doctor audits, JSON/OTLP export, opt-in sync, MCP query tools,
run filtering, run diffs, retention purge, and hook init/undo. Cargo owns the
`tranquil` executable; the Python package is retained as `tranquil-python` only
for legacy comparison while the remaining Python runtime is retired.

There is no server, no port, and no web dashboard. Hooks write straight to
SQLite, so nothing has to be running for capture to work, and a down collector
can never block or error your agent.

## Quick start

```bash
cargo install --path .
tranquil init --agent all --scope user
tranquil status --table
```

For local development without installing, run `cargo run -- status --table` or
`cargo run -- tui --once`.

`tranquil init` creates local config and wires command hooks. The setup step is
idempotent, so running it again keeps existing Tranquil-managed hooks current
without duplicating them.

`tranquil init` installs **command hooks** for both Claude Code and Codex. Each
hook runs the Rust `tranquil hook-forwarder` command, reads the hook JSON on
stdin, and writes a normalized event straight to `~/.tranquil/tranquil.db`. The
ingester is strictly fail-open: any error is
logged to stderr and it still exits 0, so the agent loop is never blocked.

While the terminal app is open it also runs the transcript and Codex rollout
tailers as a durable backfill path, so events that never fired a hook are still
captured. You can also backfill at any time with `tranquil ingest <path>`.

To enable the `outcome_judge` scorer, set `judge_command` in
`~/.tranquil/config.json`, pass `--judge-command`, or set
`TRANQUIL_JUDGE_COMMAND`. The command receives a JSON object on stdin with the
fixture prompt, rubric, reference, repo metadata, run summary, and events. It
must print a JSON object such as:

```json
{"passed": true, "score": 0.9, "reason": "rubric satisfied"}
```

Signals can be delivered outside the terminal by setting
`notification_webhook_url` or `notification_command` in
`~/.tranquil/config.json`. `TRANQUIL_NOTIFICATION_WEBHOOK_URL` and
`TRANQUIL_NOTIFICATION_COMMAND` provide the same settings from the environment.
Both receive a JSON payload with `type: "signal"` and the signal record.

Optional pre-tool guardrails can deny risky tool calls before they run. Set
`policy_enabled: true` plus `policy_forbidden_paths` or
`policy_forbidden_commands` in `~/.tranquil/config.json`. Matching `PreToolUse`
hooks emit a deny decision on stdout and create a `policy_denied` signal. The
same path enforces manual stops (`tranquil stop <run_id>`) and the optional cost
kill switch.

To continuously grow regression coverage from real runs, set
`trace_sampling_enabled: true`, `trace_sample_rate`, and optionally
`trace_sample_suite` in `~/.tranquil/config.json`. Completed runs whose stable
sample bucket is under the configured rate are saved as fixtures as they finish.
You can also run `tranquil fixture sample` manually to backfill from already
captured runs.

Team/cloud sync is push-only and opt-in. Set `sync_endpoint` in config or pass
`tranquil sync --endpoint ...`; Tranquil posts a local export to that endpoint
only when you run the command.

## Commands

```text
tranquil                       Print fleet status
tranquil app                   Open the terminal Fleet view
tranquil tui                   Open the terminal Fleet view
tranquil init                  Wire local command hooks
tranquil init --undo           Remove Tranquil-managed hook entries
tranquil doctor                Check config, SQLite, and hook wiring
tranquil doctor --codex-audit  Inspect configured Codex rollout paths and report
                               local coverage fields
tranquil status                Print fleet status
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
tranquil tui                   Terminal Fleet view with live refresh, keyboard
                               navigation, peek, and stop request
tranquil tui --run <run_id>    Terminal Run view with signals, files, scores,
                               subagents, and recent events
```

## Scope

Implemented now:

- Canonical event normalization for Claude Code and Codex-shaped payloads.
- Serverless capture: command hooks that write normalized events straight to
  SQLite, with a fail-open ingester that never blocks the agent loop.
- Idempotent, reversible Claude Code and Codex hook install/undo with a managed
  metadata block, plus MCP-server install for Claude Code.
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
- Rich terminal Fleet view with state/vendor/repo/branch/label filters, a run
  detail view with subagent summaries, file read/write counts, eval score chips,
  inline edit/write diffs, recent events, keyboard navigation, peek, and a stop
  request, all backed directly by SQLite with live refresh.
- Signals for loops, runaway cost, skipped checks, reread thrash, idle runs,
  scheduled/background idle runs, and failure cascades, with optional webhook or
  command notifications delivered synchronously from the hook ingester.
- Manual stop requests through `tranquil stop <run_id>` and the TUI, enforced by
  denying future `PreToolUse` hooks for that run.
- Optional `PreToolUse` policy guardrails for forbidden paths and command
  patterns, plus an optional run cost kill switch.
- Basic fixture and eval flow with `tests_pass`, `build_succeeds`,
  `diff_applies`, `no_loops`, and command-backed `outcome_judge` scorers.
- Fixture budgets, forbidden-path scorers, last-green regression checks,
  suite-matrix replay variants, and git-worktree replay materialization,
  including tracked dirty patches captured with fixtures.
- Production-trace sampling into replayable fixtures, manually through
  `tranquil fixture sample` or continuously as completed runs are captured.
- Appendix-style fixture and suite YAML definitions, including rubric/reference
  metadata for judge scorers, without adding a runtime YAML dependency.
- MCP stdio server exposing run query with `since` filters, run detail, run
  diff, cost rollups with `since`/`window`, signal, and eval-status tools.
- Local JSON export, OTLP/HTTP log export, opt-in sync push, and purge controls.

Both Claude Code and Codex run command hooks. `tranquil init` writes hook
entries that run `tranquil hook-forwarder`, which posts nothing over the
network; it writes directly to the local SQLite store. Codex may require
reviewing the new hooks with `/hooks` before it runs them.

## Distribution

The supported install path is Rust-first:

```bash
cargo install --path .
```

The crate exposes a single `tranquil` binary. Python packaging is kept only for
legacy comparison and installs `tranquil-python`, not `tranquil`, so
`pip install .` no longer shadows the Rust executable.
