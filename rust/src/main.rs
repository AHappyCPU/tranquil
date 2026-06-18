mod codex_audit;
mod config;
mod evals;
mod http;
mod ingest;
mod init;
mod mcp;
mod normalize;
mod notifications;
mod otel;
mod policy;
mod sqlite;
mod storage;
mod suites;
mod tailer;
mod team_sync;
mod tui;
mod util;

use std::env;
use std::io::{self, Read};
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use config::Config;
use normalize::normalize_event;
use storage::{RunFilters, Storage};

fn main() {
    let code = match run() {
        Ok(code) => code,
        Err(err) => {
            eprintln!("tranquil: {err}");
            1
        }
    };
    std::process::exit(code);
}

fn run() -> Result<i32, String> {
    let mut args: Vec<String> = env::args().skip(1).collect();
    let home = take_option_path(&mut args, "--home");
    let command = args.first().map(String::as_str).unwrap_or("status");
    match command {
        "app" | "tui" => cmd_tui(&args[1..], home),
        "tail" => cmd_tail(&args[1..], home),
        "status" => cmd_status(&args[1..], home),
        "doctor" => cmd_doctor(&args[1..], home),
        "hook-forwarder" | "hook_forwarder" | "hook" => cmd_hook(&args[1..], home),
        "export" => cmd_export(&args[1..], home),
        "sync" => cmd_sync(&args[1..], home),
        "ingest" => cmd_ingest(&args[1..], home),
        "init" => cmd_init(&args[1..], home),
        "signals" => cmd_signals(&args[1..], home),
        "stop" => cmd_stop(&args[1..], home),
        "purge" => cmd_purge(&args[1..], home),
        "mcp" => cmd_mcp(home),
        "fixture" => cmd_fixture(&args[1..], home),
        "eval" => cmd_eval(&args[1..], home),
        "replay" => cmd_replay(&args[1..], home),
        "help" | "--help" | "-h" => {
            print_help();
            Ok(0)
        }
        other => Err(format!("unknown command: {other}")),
    }
}

fn open_storage(config: &Config) -> Result<Storage, String> {
    Storage::open_with_raw_payloads(&config.db_path, config.raw_payloads)
}

fn cmd_status(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let table = args.iter().any(|arg| arg == "--table");
    let limit = option_value(args, "--limit")
        .map(|value| value.parse::<i64>())
        .transpose()
        .map_err(|err| format!("invalid --limit: {err}"))?
        .unwrap_or(20);
    let filters = run_filters_from_args(args, limit);
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    let stats = storage.stats()?;
    if !table {
        println!(
            "Tranquil {} live / {} runs | ${:.2} est. | {} signaled",
            stats.live, stats.runs, stats.cost_usd_est, stats.signaled
        );
        return Ok(0);
    }
    let runs = storage.list_runs_filtered(&filters)?;
    if runs.is_empty() {
        println!("No runs captured yet.");
        return Ok(0);
    }
    println!(
        "{:10} {:12} {:>10}  {:30} RUN",
        "STATE", "AGENT", "COST EST.", "REPO / BRANCH"
    );
    for run in runs {
        let repo = run.repo.unwrap_or_else(|| "unknown".to_string());
        let branch = run.branch.unwrap_or_else(|| "-".to_string());
        println!(
            "{:10} {:12} ${:>9.2}  {:30} {}",
            truncate(&run.status, 10),
            truncate(&run.agent, 12),
            run.total_cost_usd_est,
            truncate(&format!("{repo} / {branch}"), 30),
            run.run_id
        );
    }
    Ok(0)
}

fn cmd_tui(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    let once = args.iter().any(|arg| arg == "--once");
    let run_id = option_value(args, "--run");
    let limit = option_value(args, "--limit")
        .map(|value| value.parse::<i64>())
        .transpose()
        .map_err(|err| format!("invalid --limit: {err}"))?
        .unwrap_or(30);
    let filters = run_filters_from_args(args, limit);
    let interval = option_value(args, "--interval")
        .map(|value| value.parse::<f64>())
        .transpose()
        .map_err(|err| format!("invalid --interval: {err}"))?
        .unwrap_or(config.tail_interval_seconds);
    let mut tail_state = tailer::TailState::default();
    loop {
        let ingested = match tailer::scan_configured_once(&storage, &config, &mut tail_state) {
            Ok(count) => count,
            Err(err) => {
                eprintln!("tranquil-tail: {err}");
                0
            }
        };
        let output = if let Some(run_id) = &run_id {
            tui::render_run(&storage, run_id, limit.max(1) as usize)?
        } else {
            tui::render_fleet(&storage, &filters, ingested)?
        };
        if !once {
            print!("\x1b[2J\x1b[H");
        }
        println!("{output}");
        if once {
            break;
        }
        thread::sleep(Duration::from_secs_f64(interval.max(0.5)));
    }
    Ok(0)
}

fn cmd_tail(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    let once = args.iter().any(|arg| arg == "--once");
    let interval = option_value(args, "--interval")
        .map(|value| value.parse::<f64>())
        .transpose()
        .map_err(|err| format!("invalid --interval: {err}"))?
        .unwrap_or(config.tail_interval_seconds);
    let mut tail_state = tailer::TailState::default();
    loop {
        let count = tailer::scan_configured_once(&storage, &config, &mut tail_state)?;
        println!("ingested: {count}");
        if once {
            break;
        }
        thread::sleep(Duration::from_secs_f64(interval.max(0.5)));
    }
    Ok(0)
}

fn cmd_doctor(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let config = Config::load(home, true)?;
    println!("home: {}", config.home.display());
    println!("config: {}", config.config_path().display());
    println!("db: {}", config.db_path.display());
    let storage = open_storage(&config)?;
    let stats = storage.stats()?;
    println!("sqlite: ok runs={} signaled={}", stats.runs, stats.signaled);
    if args.iter().any(|arg| arg == "--codex-audit") {
        let report = codex_audit::audit_codex_paths(&config.codex_rollout_paths);
        println!("codex audit:");
        println!(
            "  files: {} ({} sqlite, {} json)",
            report.files, report.sqlite_files, report.json_files
        );
        println!("  event hints: {}", format_counts(&report.event_hints, 6));
        println!(
            "  has_prompt: {}",
            if report.coverage.has_prompt {
                "yes"
            } else {
                "no"
            }
        );
        println!(
            "  has_tool: {}",
            if report.coverage.has_tool {
                "yes"
            } else {
                "no"
            }
        );
        println!(
            "  has_usage: {}",
            if report.coverage.has_usage {
                "yes"
            } else {
                "no"
            }
        );
        println!(
            "  has_timestamps: {}",
            if report.coverage.has_timestamps {
                "yes"
            } else {
                "no"
            }
        );
        println!(
            "  has_sessions: {}",
            if report.coverage.has_sessions {
                "yes"
            } else {
                "no"
            }
        );
        if !report.errors.is_empty() {
            println!("  errors: {}", report.errors.len());
        }
    }
    Ok(0)
}

fn cmd_hook(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    if let Err(err) = cmd_hook_inner(args, home) {
        eprintln!("tranquil-hook: {err}");
    }
    Ok(0)
}

fn cmd_hook_inner(args: &[String], home: Option<PathBuf>) -> Result<(), String> {
    let event = option_value(args, "--event").ok_or_else(|| "--event is required".to_string())?;
    let agent = option_value(args, "--agent");
    let config = Config::load(home, true)?;
    let mut raw = String::new();
    io::stdin()
        .read_to_string(&mut raw)
        .map_err(|err| err.to_string())?;
    let mut payload: serde_json::Value = serde_json::from_str(&raw).unwrap_or_else(|_| {
        serde_json::json!({
            "stdin": raw
        })
    });
    if !payload.is_object() {
        payload = serde_json::json!({ "stdin": payload });
    }
    if let Some(agent) = agent {
        payload["agent"] = serde_json::Value::String(agent);
    }
    payload["hook_event_name"] = serde_json::Value::String(event.clone());
    let mut normalized = normalize_event(&event, &payload, "hook");
    let storage = open_storage(&config)?;
    let run_id = normalized
        .get("run_id")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
        .to_string();
    let before_signals = if run_id.is_empty() {
        std::collections::BTreeSet::new()
    } else {
        notifications::signal_ids(&storage, &run_id).unwrap_or_default()
    };
    if normalized
        .get("event_type")
        .and_then(serde_json::Value::as_str)
        == Some("pre_tool")
    {
        if let Some(reason) = policy::pre_tool_decision(&storage, &config, &normalized)? {
            normalized["permission"] = serde_json::json!({"decision": "deny", "reason": reason});
            storage.record_event_with_thresholds(&normalized, &config.signal_thresholds)?;
            if !run_id.is_empty() {
                if let Err(err) =
                    notifications::notify_new_signals(&storage, &config, &run_id, &before_signals)
                {
                    eprintln!("tranquil-hook: notification: {err}");
                }
            }
            write_decision(&reason)?;
            return Ok(());
        }
    }
    storage.record_event_with_thresholds(&normalized, &config.signal_thresholds)?;
    maybe_sample_trace(&storage, &config, &normalized);
    if !run_id.is_empty() {
        if let Err(err) =
            notifications::notify_new_signals(&storage, &config, &run_id, &before_signals)
        {
            eprintln!("tranquil-hook: notification: {err}");
        }
    }
    Ok(())
}

fn maybe_sample_trace(storage: &Storage, config: &Config, event: &serde_json::Value) {
    if !config.trace_sampling_enabled {
        return;
    }
    let event_type = event
        .get("event_type")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    if !matches!(event_type, "session_end" | "stop" | "task_completed") {
        return;
    }
    let Some(run_id) = event.get("run_id").and_then(serde_json::Value::as_str) else {
        return;
    };
    if let Err(err) = storage.sample_run_if_eligible(
        run_id,
        &config.trace_sample_suite,
        config.trace_sample_rate,
        "completed",
    ) {
        eprintln!("tranquil-hook: trace sampling: {err}");
    }
}

fn cmd_export(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let json_path = option_value(args, "--json").map(PathBuf::from);
    let otel_endpoint = option_value(args, "--otel");
    if json_path.is_none() && otel_endpoint.is_none() {
        return Err("export requires --json PATH or --otel ENDPOINT".to_string());
    }
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    if let Some(path) = json_path {
        let data = storage.export_data()?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|err| err.to_string())?;
        }
        std::fs::write(
            &path,
            serde_json::to_string_pretty(&data).map_err(|err| err.to_string())? + "\n",
        )
        .map_err(|err| err.to_string())?;
        println!("exported: {}", path.display());
    }
    if let Some(endpoint) = otel_endpoint {
        let result = otel::export_otlp_http(&storage, &endpoint, &parse_headers(args)?)?;
        println!("otel: status={} records={}", result.status, result.records);
    }
    Ok(0)
}

fn cmd_sync(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let config = Config::load(home, true)?;
    let endpoint = option_value(args, "--endpoint")
        .or(config.sync_endpoint.clone())
        .ok_or_else(|| "sync requires --endpoint or sync_endpoint in config.json".to_string())?;
    let storage = open_storage(&config)?;
    let mut headers = config
        .sync_headers
        .iter()
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect::<Vec<_>>();
    headers.extend(parse_headers(args)?);
    let result = team_sync::push_sync(&storage, &endpoint, &headers)?;
    println!(
        "sync: status={} runs={} events={} fixtures={} scores={}",
        result.status, result.runs, result.events, result.fixtures, result.scores
    );
    Ok(0)
}

fn cmd_ingest(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let path = args
        .first()
        .ok_or_else(|| "ingest requires PATH".to_string())?;
    let agent = option_value(args, "--agent");
    let limit = option_value(args, "--limit")
        .map(|value| value.parse::<usize>())
        .transpose()
        .map_err(|err| format!("invalid --limit: {err}"))?;
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    let count = ingest::ingest_path(
        &storage,
        &PathBuf::from(path),
        agent.as_deref(),
        limit,
        &config.signal_thresholds,
    )?;
    println!("ingested: {count}");
    Ok(0)
}

fn cmd_init(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let config = Config::load(home, true)?;
    let agent = option_value(args, "--agent").unwrap_or_else(|| "all".to_string());
    let scope = option_value(args, "--scope").unwrap_or_else(|| "user".to_string());
    let undo = args.iter().any(|arg| arg == "--undo");
    let cwd = env::current_dir().map_err(|err| err.to_string())?;
    let report = init::run_init(&config, &agent, &scope, undo, &cwd)?;
    report.print();
    Ok(0)
}

fn cmd_signals(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    let active = if args.iter().any(|arg| arg == "--all") {
        None
    } else {
        Some(true)
    };
    let signals = storage.list_signals(active, 100)?;
    if signals.is_empty() {
        println!("No signals.");
        return Ok(0);
    }
    for signal in signals {
        println!(
            "{:6} {:18} {} {}",
            signal.severity,
            signal.signal_type,
            signal.run_id,
            serde_json::to_string(&signal.evidence).map_err(|err| err.to_string())?
        );
    }
    Ok(0)
}

fn cmd_stop(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let run_id = args
        .first()
        .ok_or_else(|| "stop requires RUN_ID".to_string())?;
    let reason =
        option_value(args, "--reason").unwrap_or_else(|| "user_requested_stop".to_string());
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    let inserted = storage.request_stop(run_id, &reason)?;
    println!(
        "stop requested: {run_id}{}",
        if inserted { "" } else { " (already requested)" }
    );
    Ok(0)
}

fn cmd_purge(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let all = args.iter().any(|arg| arg == "--all");
    let older_than = option_value(args, "--older-than")
        .map(|value| value.parse::<i64>())
        .transpose()
        .map_err(|err| format!("invalid --older-than: {err}"))?;
    if !all && older_than.is_none() {
        return Err("purge requires --all or --older-than DAYS".to_string());
    }
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    let counts = if all {
        storage.purge(true)?
    } else {
        storage.purge_older_than(older_than.unwrap())?
    };
    println!("events: {}", counts.events);
    println!("runs: {}", counts.runs);
    println!("signals: {}", counts.signals);
    println!("fixtures: {}", counts.fixtures);
    println!("eval_runs: {}", counts.eval_runs);
    println!("scores: {}", counts.scores);
    Ok(0)
}

fn cmd_mcp(home: Option<PathBuf>) -> Result<i32, String> {
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    mcp::run_mcp_server(&storage)
}

fn cmd_fixture(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let subcommand = args
        .first()
        .map(String::as_str)
        .ok_or_else(|| "fixture requires add or list".to_string())?;
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    match subcommand {
        "add" => {
            let run_id = args
                .get(1)
                .ok_or_else(|| "fixture add requires RUN_ID".to_string())?;
            let suite = option_value(args, "--suite").unwrap_or_else(|| "default".to_string());
            let cost_budget = option_value(args, "--cost-budget")
                .map(|value| value.parse::<f64>())
                .transpose()
                .map_err(|err| format!("invalid --cost-budget: {err}"))?;
            let forbid = option_values(args, "--forbid");
            let fixture = if cost_budget.is_some() || !forbid.is_empty() {
                storage.create_fixture_with_options(
                    run_id,
                    &suite,
                    None,
                    cost_budget,
                    if forbid.is_empty() {
                        None
                    } else {
                        Some(forbid)
                    },
                    None,
                )?
            } else {
                storage.create_fixture(run_id, &suite, None)?
            };
            println!("fixture: {}", fixture.fixture_id);
            println!("suite: {}", fixture.suite);
        }
        "list" => {
            let suite = option_value(args, "--suite");
            let fixtures = storage.list_fixtures(suite.as_deref())?;
            if fixtures.is_empty() {
                println!("No fixtures.");
            } else {
                for fixture in fixtures {
                    println!(
                        "{} suite={} run={} prompt={}",
                        fixture.fixture_id,
                        fixture.suite,
                        fixture.run_id,
                        truncate(fixture.prompt.as_deref().unwrap_or(""), 72)
                    );
                }
            }
        }
        "derive" => {
            let suite = option_value(args, "--suite").unwrap_or_else(|| "signals".to_string());
            let fixtures = storage.create_fixtures_from_signals(&suite)?;
            if fixtures.is_empty() {
                println!("No signaled runs need fixtures.");
            } else {
                for fixture in fixtures {
                    println!(
                        "fixture: {} suite={} run={}",
                        fixture.fixture_id, fixture.suite, fixture.run_id
                    );
                }
            }
        }
        "sample" => {
            let suite = option_value(args, "--suite").unwrap_or_else(|| "sampled".to_string());
            let rate = option_value(args, "--rate")
                .map(|value| value.parse::<f64>())
                .transpose()
                .map_err(|err| format!("invalid --rate: {err}"))?
                .unwrap_or(1.0);
            if !(0.0..=1.0).contains(&rate) {
                return Err("--rate must be between 0.0 and 1.0".to_string());
            }
            let limit = option_value(args, "--limit")
                .map(|value| value.parse::<i64>())
                .transpose()
                .map_err(|err| format!("invalid --limit: {err}"))?
                .unwrap_or(20);
            let status = option_value(args, "--status").unwrap_or_else(|| "completed".to_string());
            let mut filters = run_filters_from_args(args, limit.max(1) * 20);
            filters.status = Some(status.clone());
            let fixtures = storage.sample_runs_filtered(&suite, rate, limit, &filters, &status)?;
            if fixtures.is_empty() {
                println!("No completed runs sampled.");
            } else {
                for fixture in fixtures {
                    println!(
                        "fixture: {} suite={} run={}",
                        fixture.fixture_id, fixture.suite, fixture.run_id
                    );
                }
            }
        }
        "import" => {
            let path = args
                .get(1)
                .ok_or_else(|| "fixture import requires PATH".to_string())?;
            let suite = option_value(args, "--suite");
            let fixture =
                suites::import_fixture_file(&storage, &PathBuf::from(path), suite.as_deref())?;
            println!("fixture: {}", fixture.fixture_id);
            println!("suite: {}", fixture.suite);
        }
        other => return Err(format!("unknown fixture command: {other}")),
    }
    Ok(0)
}

fn cmd_eval(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let mut suite = first_positional(args, &["--scorer", "--baseline", "--judge-command"])
        .unwrap_or_else(|| "default".to_string());
    let mut scorers = option_values(args, "--scorer");
    let mut baseline = option_value(args, "--baseline");
    let judge_command = option_value(args, "--judge-command");
    let config = Config::load(home, true)?;
    let storage = open_storage(&config)?;
    let mut matrix: Vec<serde_json::Value> = Vec::new();
    let suite_path = PathBuf::from(&suite);
    if suite.ends_with(".yaml") || suite.ends_with(".yml") || suite_path.exists() {
        let (suite_def, imported) = suites::import_suite_fixtures(&storage, &suite_path)?;
        suite = suite_def.suite;
        if baseline.is_none() {
            baseline = suite_def.baseline;
        }
        if scorers.is_empty() {
            scorers = suite_def.scorers;
        }
        matrix = suite_def.matrix;
        println!("suite: {suite}");
        println!("imported fixtures: {}", imported.len());
        if !matrix.is_empty() {
            println!("matrix entries: {}", matrix.len());
        }
        if !scorers.is_empty() {
            println!("scorers: {}", scorers.join(", "));
        }
    }
    let judge_command = judge_command.or(config.judge_command.clone());
    let (eval_run_id, scores) = evals::run_eval(
        &storage,
        &suite,
        &scorers,
        baseline.as_deref(),
        judge_command.as_deref(),
    )?;
    println!("eval: {eval_run_id}");
    if scores.is_empty() {
        println!("No fixtures.");
        return Ok(1);
    }
    let mut failed = false;
    for score in scores {
        failed = failed || !score.passed;
        println!(
            "{} {} {} value={}",
            if score.passed { "PASS" } else { "FAIL" },
            score.fixture_id,
            score.scorer,
            score
                .value
                .map(|value| value.to_string())
                .unwrap_or_else(|| "null".to_string())
        );
    }
    if !matrix.is_empty() {
        let matrix_results = evals::run_eval_matrix(
            &storage,
            &suite,
            &matrix,
            &config.home.join("replays"),
            config.replay_command.as_deref(),
        )?;
        for result in matrix_results {
            let variant = result
                .get("variant")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("variant");
            let fixture_id = result
                .get("fixture_id")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("");
            let matrix_eval_id = result
                .get("eval_run_id")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("");
            println!("matrix eval: {variant} fixture={fixture_id} eval={matrix_eval_id}");
            if let Some(scores) = result.get("scores").and_then(serde_json::Value::as_array) {
                for score in scores {
                    let passed = score
                        .get("passed")
                        .and_then(serde_json::Value::as_bool)
                        .unwrap_or(false);
                    failed = failed || !passed;
                    println!(
                        "{} {} {} value={}",
                        if passed { "PASS" } else { "FAIL" },
                        score
                            .get("fixture_id")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or(""),
                        score
                            .get("scorer")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or(""),
                        score
                            .get("value")
                            .map(|value| value.to_string())
                            .unwrap_or_else(|| "null".to_string())
                    );
                }
            }
        }
    }
    Ok(if failed { 1 } else { 0 })
}

fn cmd_replay(args: &[String], home: Option<PathBuf>) -> Result<i32, String> {
    let fixture_id = args
        .first()
        .ok_or_else(|| "replay requires FIXTURE_ID".to_string())?;
    let config = Config::load(home, true)?;
    let command = option_value(args, "--command")
        .or_else(|| option_value(args, "-c"))
        .or(config.replay_command.clone());
    let agent = option_value(args, "--agent").unwrap_or_else(|| "command".to_string());
    let model = option_value(args, "--model");
    let replay_config = option_value(args, "--config");
    if command.is_none() && agent == "command" {
        return Err(
            "Replay requires --command, replay_command in config.json, or --agent codex."
                .to_string(),
        );
    }
    let storage = open_storage(&config)?;
    let (eval_run_id, scores) = evals::replay_fixture_with_options(
        &storage,
        fixture_id,
        command.as_deref(),
        &config.home.join("replays"),
        &agent,
        model.as_deref(),
        replay_config.as_deref(),
        None,
        None,
    )?;
    println!("replay eval: {eval_run_id}");
    let mut failed = false;
    for score in scores {
        failed = failed || !score.passed;
        println!(
            "{} {} value={} detail={}",
            if score.passed { "PASS" } else { "FAIL" },
            score.scorer,
            score
                .value
                .map(|value| value.to_string())
                .unwrap_or_else(|| "null".to_string()),
            serde_json::to_string(&score.detail).map_err(|err| err.to_string())?
        );
    }
    Ok(if failed { 1 } else { 0 })
}

fn write_decision(reason: &str) -> Result<(), String> {
    println!(
        "{}",
        serde_json::to_string(&serde_json::json!({
            "decision": "block",
            "reason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }))
        .map_err(|err| err.to_string())?
    );
    Ok(())
}

fn take_option_path(args: &mut Vec<String>, name: &str) -> Option<PathBuf> {
    let pos = args.iter().position(|arg| arg == name)?;
    args.remove(pos);
    if pos < args.len() {
        Some(PathBuf::from(args.remove(pos)))
    } else {
        None
    }
}

fn option_value(args: &[String], name: &str) -> Option<String> {
    args.windows(2)
        .find(|pair| pair[0] == name)
        .map(|pair| pair[1].clone())
}

fn option_values(args: &[String], name: &str) -> Vec<String> {
    args.windows(2)
        .filter(|pair| pair[0] == name)
        .map(|pair| pair[1].clone())
        .collect()
}

fn run_filters_from_args(args: &[String], limit: i64) -> RunFilters {
    let mut labels = option_values(args, "--label");
    if let Some(value) = option_value(args, "--labels") {
        labels.extend(
            value
                .split(',')
                .map(str::trim)
                .filter(|label| !label.is_empty())
                .map(ToString::to_string),
        );
    }
    RunFilters {
        limit,
        status: option_value(args, "--status"),
        agent: option_value(args, "--agent"),
        repo: option_value(args, "--repo"),
        branch: option_value(args, "--branch"),
        labels,
        since: option_value(args, "--since"),
    }
}

fn parse_headers(args: &[String]) -> Result<Vec<(String, String)>, String> {
    option_values(args, "--header")
        .into_iter()
        .map(|value| {
            let (key, header_value) = value
                .split_once('=')
                .ok_or_else(|| format!("header must be Name=Value: {value}"))?;
            if key.is_empty() {
                return Err("header name cannot be empty".to_string());
            }
            Ok((key.to_string(), header_value.to_string()))
        })
        .collect()
}

fn format_counts(values: &std::collections::BTreeMap<String, usize>, limit: usize) -> String {
    if values.is_empty() {
        return "none".to_string();
    }
    let mut ordered = values.iter().collect::<Vec<_>>();
    ordered.sort_by(|left, right| right.1.cmp(left.1).then_with(|| left.0.cmp(right.0)));
    ordered
        .into_iter()
        .take(limit)
        .map(|(key, count)| format!("{key}={count}"))
        .collect::<Vec<_>>()
        .join(", ")
}

fn first_positional(args: &[String], value_options: &[&str]) -> Option<String> {
    let mut skip_next = false;
    for arg in args {
        if skip_next {
            skip_next = false;
            continue;
        }
        if value_options.contains(&arg.as_str()) {
            skip_next = true;
            continue;
        }
        if !arg.starts_with('-') {
            return Some(arg.clone());
        }
    }
    None
}

fn truncate(value: &str, max: usize) -> String {
    if value.chars().count() <= max {
        value.to_string()
    } else {
        value
            .chars()
            .take(max.saturating_sub(1))
            .collect::<String>()
            + "."
    }
}

fn print_help() {
    println!(
        "tranquil (Rust)\n\nCommands:\n  app|tui [--once] [--run RUN_ID] [--interval SEC] [--limit N] [--agent A] [--repo R] [--branch B] [--status S] [--label K[=V]]\n  tail [--once] [--interval SEC]\n  status [--table] [--limit N] [--agent A] [--repo R] [--branch B] [--status S] [--label K[=V]]\n  signals [--all]\n  stop RUN_ID [--reason REASON]\n  fixture add RUN_ID [--suite SUITE] [--cost-budget USD] [--forbid GLOB]\n  fixture list [--suite SUITE]\n  fixture import PATH [--suite SUITE]\n  fixture derive [--suite SUITE]\n  fixture sample [--suite SUITE] [--rate RATE] [--limit N] [--agent A] [--repo R] [--branch B] [--label K[=V]]\n  eval [SUITE|suite.yaml] [--baseline BASE] [--scorer SCORER] [--judge-command CMD]\n  replay FIXTURE_ID [--command COMMAND] [--agent command|codex] [--model MODEL]\n  ingest PATH [--agent AGENT] [--limit N]\n  doctor [--codex-audit]\n  hook-forwarder --event EVENT [--agent AGENT]\n  export --json PATH\n  export --otel ENDPOINT [--header Name=Value]\n  sync --endpoint ENDPOINT [--header Name=Value]\n  purge --all | --older-than DAYS\n  mcp\n  init [--agent all|claude-code|codex] [--scope user|project|local] [--undo]\n\nGlobal:\n  --home PATH"
    );
}
