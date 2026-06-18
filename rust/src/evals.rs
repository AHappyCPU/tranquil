use std::collections::{BTreeSet, HashMap};
use std::path::Path;
use std::process::Command;

use serde_json::{Value, json};

use crate::storage::{FixtureRecord, Storage, extract_tool_command};
use crate::util::{CommandResult, command_join, run_shell_command};

const SUPPORTED_SCORERS: &[&str] = &[
    "no_tool_failures",
    "tests_pass",
    "build_succeeds",
    "diff_applies",
    "no_loops",
    "cost_recorded",
    "checks_if_shipped",
    "cost_budget",
    "latency_budget",
    "no_forbidden_paths",
    "outcome_judge",
];

#[derive(Debug, Clone)]
struct ScoreDraft {
    scorer: String,
    value: Option<f64>,
    passed: bool,
    detail: Value,
}

#[derive(Debug, Clone)]
struct RegressionDraft {
    fixture_id: String,
    scorer: String,
    value: Option<f64>,
    passed: bool,
    detail: Value,
}

pub fn run_eval(
    storage: &Storage,
    suite: &str,
    requested_scorers: &[String],
    baseline: Option<&str>,
    judge_command: Option<&str>,
) -> Result<(String, Vec<crate::storage::ScoreRecord>), String> {
    let fixtures = storage.list_fixtures(Some(suite))?;
    let baseline_eval_run_id = resolve_baseline_eval_run(storage, suite, baseline)?;
    let eval_run_id = storage.create_eval_run(suite, "running", baseline_eval_run_id.as_deref())?;
    if fixtures.is_empty() {
        storage.finish_eval_run(&eval_run_id, "no_fixtures")?;
        return Ok((eval_run_id, Vec::new()));
    }
    let mut all_passed = true;
    for fixture in fixtures {
        let run = storage.get_run(&fixture.run_id)?;
        let scores = score_fixture(&fixture, run.as_ref(), requested_scorers, judge_command)?;
        for score in scores {
            all_passed = all_passed && score.passed;
            storage.add_score(
                &eval_run_id,
                &fixture.fixture_id,
                &score.scorer,
                score.value,
                score.passed,
                score.detail,
            )?;
        }
    }
    if let Some(baseline_eval_run_id) = baseline_eval_run_id {
        for regression in compare_to_baseline(storage, &eval_run_id, &baseline_eval_run_id)? {
            all_passed = false;
            storage.add_score(
                &eval_run_id,
                &regression.fixture_id,
                &regression.scorer,
                regression.value,
                regression.passed,
                regression.detail,
            )?;
        }
    }
    storage.finish_eval_run(&eval_run_id, if all_passed { "passed" } else { "failed" })?;
    let scores = storage.list_scores(&eval_run_id)?;
    Ok((eval_run_id, scores))
}

#[allow(dead_code)]
pub fn replay_fixture(
    storage: &Storage,
    fixture_id: &str,
    command: &str,
    replay_root: &Path,
) -> Result<(String, Vec<crate::storage::ScoreRecord>), String> {
    replay_fixture_with_options(
        storage,
        fixture_id,
        Some(command),
        replay_root,
        "command",
        None,
        None,
        None,
        None,
    )
}

pub fn replay_fixture_with_options(
    storage: &Storage,
    fixture_id: &str,
    command: Option<&str>,
    replay_root: &Path,
    agent: &str,
    model: Option<&str>,
    config_path: Option<&str>,
    suite: Option<&str>,
    matrix_variant: Option<&Value>,
) -> Result<(String, Vec<crate::storage::ScoreRecord>), String> {
    let fixture = storage
        .get_fixture(fixture_id)?
        .ok_or_else(|| format!("fixture not found: {fixture_id}"))?;
    let eval_run_id = storage.create_eval_run(suite.unwrap_or(&fixture.suite), "running", None)?;
    let workdir = replay_root.join(&eval_run_id);
    std::fs::create_dir_all(&workdir).map_err(|err| err.to_string())?;
    let prompt_path = workdir.join("prompt.txt");
    let fixture_path = workdir.join("fixture.json");
    std::fs::write(&prompt_path, fixture.prompt.clone().unwrap_or_default())
        .map_err(|err| err.to_string())?;
    std::fs::write(
        &fixture_path,
        serde_json::to_string_pretty(&fixture).map_err(|err| err.to_string())?,
    )
    .map_err(|err| err.to_string())?;
    let (repo_dir, materialized) = materialize_repo(&fixture.repo_ref, &workdir)?;
    let materialized_passed = materialized
        .get("passed")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    storage.add_score(
        &eval_run_id,
        fixture_id,
        "repo_materialized",
        Some(if materialized_passed { 1.0 } else { 0.0 }),
        materialized_passed,
        materialized,
    )?;
    if let Some(matrix_variant) = matrix_variant {
        storage.add_score(
            &eval_run_id,
            fixture_id,
            "matrix_variant",
            Some(1.0),
            true,
            json!({"variant": matrix_variant}),
        )?;
    }
    let owned_command;
    let command = if let Some(command) = command {
        command
    } else {
        owned_command =
            replay_command_for_agent(agent, fixture.prompt.as_deref().unwrap_or(""), model)
                .ok_or_else(|| "replay requires --command or --agent codex".to_string())?;
        &owned_command
    };
    let result = run_replay_command(
        command,
        &fixture,
        &workdir,
        &repo_dir,
        &prompt_path,
        &fixture_path,
        model,
        config_path,
    )?;
    std::fs::write(workdir.join("stdout.txt"), &result.stdout).map_err(|err| err.to_string())?;
    std::fs::write(workdir.join("stderr.txt"), &result.stderr).map_err(|err| err.to_string())?;
    let passed = result.status == 0;
    storage.add_score(
        &eval_run_id,
        fixture_id,
        "replay_command_exits_zero",
        Some(result.status as f64),
        passed,
        json!({
            "command": command,
            "agent": agent,
            "model": model,
            "config_path": config_path,
            "returncode": result.status,
            "workdir": workdir,
            "repo_dir": repo_dir,
            "stdout_path": workdir.join("stdout.txt"),
            "stderr_path": workdir.join("stderr.txt"),
        }),
    )?;
    storage.finish_eval_run(
        &eval_run_id,
        if passed && materialized_passed {
            "passed"
        } else {
            "failed"
        },
    )?;
    let scores = storage.list_scores(&eval_run_id)?;
    Ok((eval_run_id, scores))
}

pub fn run_eval_matrix(
    storage: &Storage,
    suite: &str,
    matrix: &[Value],
    replay_root: &Path,
    default_command: Option<&str>,
) -> Result<Vec<Value>, String> {
    let fixtures = storage.list_fixtures(Some(suite))?;
    let mut results = Vec::new();
    for (index, raw_entry) in matrix.iter().enumerate() {
        let entry = raw_entry.as_object().cloned().unwrap_or_else(|| {
            serde_json::Map::from_iter([("name".to_string(), raw_entry.clone())])
        });
        let variant = matrix_variant_name(&Value::Object(entry.clone()), index);
        let variant_suite = format!("{suite}:{variant}");
        for fixture in &fixtures {
            let command = entry
                .get("command")
                .or_else(|| entry.get("replay_command"))
                .and_then(Value::as_str)
                .or(default_command);
            let agent = entry
                .get("agent")
                .and_then(Value::as_str)
                .unwrap_or("command");
            let model = entry.get("model").and_then(Value::as_str);
            let config_path = entry.get("config").and_then(Value::as_str);
            let (eval_run_id, scores) = if command.is_none() && agent == "command" {
                let eval_run_id = storage.create_eval_run(&variant_suite, "running", None)?;
                storage.add_score(
                    &eval_run_id,
                    &fixture.fixture_id,
                    "matrix_replay_configured",
                    None,
                    false,
                    json!({
                        "variant": entry,
                        "reason": "matrix entry requires command/replay_command, default replay_command, or agent=codex",
                    }),
                )?;
                storage.finish_eval_run(&eval_run_id, "failed")?;
                let scores = storage.list_scores(&eval_run_id)?;
                (eval_run_id, scores)
            } else {
                replay_fixture_with_options(
                    storage,
                    &fixture.fixture_id,
                    command,
                    replay_root,
                    agent,
                    model,
                    config_path,
                    Some(&variant_suite),
                    Some(&Value::Object(entry.clone())),
                )?
            };
            results.push(json!({
                "variant": variant,
                "suite": variant_suite,
                "fixture_id": fixture.fixture_id,
                "eval_run_id": eval_run_id,
                "scores": scores,
            }));
        }
    }
    Ok(results)
}

fn run_replay_command(
    command: &str,
    fixture: &FixtureRecord,
    workdir: &Path,
    repo_dir: &Path,
    prompt_path: &Path,
    fixture_path: &Path,
    model: Option<&str>,
    config_path: Option<&str>,
) -> Result<CommandResult, String> {
    let mut envs = vec![
        ("TRANQUIL_FIXTURE_ID", fixture.fixture_id.clone()),
        (
            "TRANQUIL_FIXTURE_PROMPT",
            fixture.prompt.clone().unwrap_or_default(),
        ),
        ("TRANQUIL_FIXTURE_FILE", fixture_path.display().to_string()),
        ("TRANQUIL_PROMPT_FILE", prompt_path.display().to_string()),
        ("TRANQUIL_REPLAY_DIR", workdir.display().to_string()),
        ("TRANQUIL_REPO_DIR", repo_dir.display().to_string()),
    ];
    if let Some(model) = model {
        envs.push(("TRANQUIL_REPLAY_MODEL", model.to_string()));
    }
    if let Some(config_path) = config_path {
        envs.push(("TRANQUIL_REPLAY_CONFIG", config_path.to_string()));
    }
    run_shell_command(command, Some(repo_dir), &envs, None)
}

fn replay_command_for_agent(agent: &str, prompt: &str, model: Option<&str>) -> Option<String> {
    if agent != "codex" {
        return None;
    }
    let mut parts = vec![
        "codex".to_string(),
        "exec".to_string(),
        "--sandbox".to_string(),
        "workspace-write".to_string(),
    ];
    if let Some(model) = model {
        parts.extend(["--model".to_string(), model.to_string()]);
    }
    parts.push(prompt.to_string());
    Some(command_join(&parts))
}

fn matrix_variant_name(entry: &Value, index: usize) -> String {
    let raw = entry
        .get("name")
        .or_else(|| entry.get("id"))
        .or_else(|| entry.get("model"))
        .or_else(|| entry.get("agent"))
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .unwrap_or_else(|| format!("variant-{}", index + 1));
    let name = raw
        .trim()
        .to_ascii_lowercase()
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.') {
                ch
            } else {
                '-'
            }
        })
        .collect::<String>()
        .trim_matches('-')
        .to_string();
    if name.is_empty() {
        format!("variant-{}", index + 1)
    } else {
        name
    }
}

fn materialize_repo(
    repo_ref: &Value,
    workdir: &Path,
) -> Result<(std::path::PathBuf, Value), String> {
    let repo_dir = workdir.join("repo");
    let git_root = repo_ref.get("git_root").and_then(Value::as_str);
    let sha = repo_ref.get("sha").and_then(Value::as_str);
    if let (Some(git_root), Some(sha)) = (git_root, sha) {
        if Path::new(git_root).exists() {
            let output = Command::new("git")
                .args(["-C", git_root, "worktree", "add", "--detach"])
                .arg(&repo_dir)
                .arg(sha)
                .output()
                .map_err(|err| err.to_string())?;
            std::fs::write(
                workdir.join("worktree.stdout.txt"),
                String::from_utf8_lossy(&output.stdout).as_ref(),
            )
            .map_err(|err| err.to_string())?;
            std::fs::write(
                workdir.join("worktree.stderr.txt"),
                String::from_utf8_lossy(&output.stderr).as_ref(),
            )
            .map_err(|err| err.to_string())?;
            if !output.status.success() {
                std::fs::create_dir_all(&repo_dir).map_err(|err| err.to_string())?;
            }
            let mut detail = json!({
                "passed": output.status.success(),
                "strategy": "git_worktree",
                "git_root": git_root,
                "sha": sha,
                "branch": repo_ref.get("branch").cloned().unwrap_or(Value::Null),
                "dirty_at_capture": repo_ref.get("dirty").and_then(Value::as_bool).unwrap_or(false),
                "returncode": output.status.code().unwrap_or(-1),
                "stdout_path": workdir.join("worktree.stdout.txt"),
                "stderr_path": workdir.join("worktree.stderr.txt"),
            });
            if output.status.success() {
                if let Some(dirty_patch) = repo_ref
                    .get("dirty_patch")
                    .and_then(Value::as_str)
                    .filter(|patch| !patch.is_empty())
                {
                    let patch_path = workdir.join("dirty.patch");
                    std::fs::write(&patch_path, dirty_patch).map_err(|err| err.to_string())?;
                    let applied = Command::new("git")
                        .arg("-C")
                        .arg(&repo_dir)
                        .args(["apply", "--whitespace=nowarn"])
                        .arg(&patch_path)
                        .output()
                        .map_err(|err| err.to_string())?;
                    std::fs::write(
                        workdir.join("dirty-apply.stdout.txt"),
                        String::from_utf8_lossy(&applied.stdout).as_ref(),
                    )
                    .map_err(|err| err.to_string())?;
                    std::fs::write(
                        workdir.join("dirty-apply.stderr.txt"),
                        String::from_utf8_lossy(&applied.stderr).as_ref(),
                    )
                    .map_err(|err| err.to_string())?;
                    detail["dirty_patch_path"] = json!(patch_path);
                    detail["dirty_patch_applied"] = json!(applied.status.success());
                    detail["dirty_patch_returncode"] = json!(applied.status.code().unwrap_or(-1));
                    detail["dirty_status"] =
                        repo_ref.get("dirty_status").cloned().unwrap_or(Value::Null);
                    detail["dirty_apply_stdout_path"] =
                        json!(workdir.join("dirty-apply.stdout.txt"));
                    detail["dirty_apply_stderr_path"] =
                        json!(workdir.join("dirty-apply.stderr.txt"));
                    detail["passed"] = json!(
                        detail["passed"].as_bool().unwrap_or(false) && applied.status.success()
                    );
                }
            }
            return Ok((repo_dir, detail));
        }
    }
    if let Some(cwd) = repo_ref.get("cwd").and_then(Value::as_str) {
        if Path::new(cwd).exists() {
            std::fs::create_dir_all(&repo_dir).map_err(|err| err.to_string())?;
            return Ok((
                repo_dir,
                json!({
                    "passed": true,
                    "strategy": "empty_replay_dir_with_original_cwd_reference",
                    "cwd": cwd,
                    "reason": "fixture has no git SHA; original cwd is recorded but not copied",
                }),
            ));
        }
    }
    std::fs::create_dir_all(&repo_dir).map_err(|err| err.to_string())?;
    Ok((
        repo_dir,
        json!({
            "passed": true,
            "strategy": "empty_replay_dir",
            "reason": "fixture has no repo reference",
        }),
    ))
}

fn score_fixture(
    fixture: &FixtureRecord,
    run: Option<&crate::storage::RunSummary>,
    requested_scorers: &[String],
    judge_command: Option<&str>,
) -> Result<Vec<ScoreDraft>, String> {
    let events = &fixture.recorded_trajectory;
    let failures = events
        .iter()
        .filter(|event| event.get("event_type").and_then(Value::as_str) == Some("tool_failure"))
        .count();
    let repeated = repeated_tool_inputs(events);
    let max_repeated = repeated.values().copied().max().unwrap_or(0);
    let cost = run.map(|run| run.total_cost_usd_est).unwrap_or_else(|| {
        events
            .iter()
            .map(|event| {
                event
                    .get("usage")
                    .and_then(|usage| usage.get("cost_usd_est"))
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0)
            })
            .sum()
    });
    let produced = run.is_some_and(|run| run.produced_pr);
    let checks = run.is_some_and(|run| run.checks_ran) || events.iter().any(event_has_check);
    let budgets = fixture
        .repo_ref
        .get("budgets")
        .filter(|value| value.is_object())
        .unwrap_or(&Value::Null);
    let forbidden_paths = fixture
        .repo_ref
        .get("forbidden_paths")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(ToString::to_string)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let requested = requested_scorers
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    let mut scores = vec![
        ScoreDraft {
            scorer: "no_tool_failures".to_string(),
            value: Some(failures as f64),
            passed: failures == 0,
            detail: json!({"failures": failures}),
        },
        score_command_exit_succeeds("tests_pass", events, command_looks_like_test, "test"),
        score_command_exit_succeeds("build_succeeds", events, command_looks_like_build, "build"),
        score_diff_applies(events),
        ScoreDraft {
            scorer: "no_loops".to_string(),
            value: Some(max_repeated as f64),
            passed: max_repeated < 3,
            detail: json!({"repeated_tool_inputs": repeated}),
        },
        ScoreDraft {
            scorer: "cost_recorded".to_string(),
            value: Some(cost),
            passed: cost >= 0.0,
            detail: json!({"cost_usd_est": cost}),
        },
        ScoreDraft {
            scorer: "checks_if_shipped".to_string(),
            value: Some(if checks { 1.0 } else { 0.0 }),
            passed: !produced || checks,
            detail: json!({"produced_pr_or_commit": produced, "checks_ran": checks}),
        },
    ];
    if budgets.get("cost_usd").is_some() || requested.contains("cost_budget") {
        let budget = budgets.get("cost_usd").and_then(Value::as_f64);
        scores.push(ScoreDraft {
            scorer: "cost_budget".to_string(),
            value: Some(cost),
            passed: budget.is_none_or(|budget| cost <= budget),
            detail: json!({
                "status": if budget.is_some() { "scored" } else { "not_applicable" },
                "cost_usd_est": cost,
                "budget_usd": budget,
            }),
        });
    }
    if budgets.get("wall_clock_s").is_some() || requested.contains("latency_budget") {
        let budget = budgets.get("wall_clock_s").and_then(Value::as_f64);
        let latency = run.and_then(run_wall_clock_s);
        scores.push(ScoreDraft {
            scorer: "latency_budget".to_string(),
            value: latency,
            passed: budget.is_none_or(|budget| latency.is_some_and(|latency| latency <= budget)),
            detail: json!({
                "status": if budget.is_some() { "scored" } else { "not_applicable" },
                "wall_clock_s": latency,
                "budget_s": budget,
            }),
        });
    }
    if !forbidden_paths.is_empty() || requested.contains("no_forbidden_paths") {
        let touched = events
            .iter()
            .flat_map(crate::storage::extract_paths)
            .collect::<BTreeSet<_>>();
        let violations = touched
            .iter()
            .filter(|path| {
                forbidden_paths
                    .iter()
                    .any(|pattern| wildcard_match(pattern, path))
            })
            .cloned()
            .collect::<Vec<_>>();
        scores.push(ScoreDraft {
            scorer: "no_forbidden_paths".to_string(),
            value: Some(violations.len() as f64),
            passed: violations.is_empty(),
            detail: json!({
                "status": if forbidden_paths.is_empty() { "not_applicable" } else { "scored" },
                "forbidden_paths": forbidden_paths,
                "violations": violations,
            }),
        });
    }
    if requested.contains("outcome_judge") {
        scores.push(score_outcome_judge(fixture, run, events, judge_command)?);
    }
    if requested_scorers.is_empty() {
        return Ok(scores);
    }
    scores.retain(|score| requested.contains(score.scorer.as_str()));
    for scorer in requested_scorers {
        if !scores.iter().any(|score| score.scorer == *scorer) {
            if !SUPPORTED_SCORERS.contains(&scorer.as_str()) {
                scores.push(ScoreDraft {
                    scorer: scorer.clone(),
                    value: None,
                    passed: false,
                    detail: json!({"status": "unsupported", "supported_scorers": SUPPORTED_SCORERS}),
                });
            }
        }
    }
    Ok(scores)
}

fn score_command_exit_succeeds(
    scorer: &str,
    events: &[Value],
    predicate: fn(&str) -> bool,
    command_kind: &str,
) -> ScoreDraft {
    let commands = matching_command_events(events, predicate);
    if commands.is_empty() {
        return ScoreDraft {
            scorer: scorer.to_string(),
            value: None,
            passed: true,
            detail: json!({"status": "not_applicable", "reason": format!("no_{command_kind}_command_captured"), "commands": []}),
        };
    }
    let failures = commands
        .iter()
        .filter(|command| {
            command["event_type"] == "tool_failure"
                || command
                    .get("exit_code")
                    .and_then(Value::as_i64)
                    .is_some_and(|code| code != 0)
        })
        .cloned()
        .collect::<Vec<_>>();
    let passed = failures.is_empty();
    ScoreDraft {
        scorer: scorer.to_string(),
        value: if passed { Some(1.0) } else { Some(0.0) },
        passed,
        detail: json!({
            "status": if passed { "passed" } else { "failed" },
            "commands": commands,
            "failures": failures,
        }),
    }
}

fn matching_command_events(events: &[Value], predicate: fn(&str) -> bool) -> Vec<Value> {
    events
        .iter()
        .filter(|event| {
            matches!(
                event.get("event_type").and_then(Value::as_str),
                Some("post_tool" | "tool_failure")
            )
        })
        .filter_map(|event| {
            let command = extract_tool_command(event)?;
            if !predicate(&command) {
                return None;
            }
            let tool = event.get("tool").unwrap_or(&Value::Null);
            Some(json!({
                "event_id": event.get("event_id").cloned().unwrap_or(Value::Null),
                "event_type": event.get("event_type").cloned().unwrap_or(Value::Null),
                "tool": tool.get("name").cloned().unwrap_or(Value::Null),
                "command": command,
                "exit_code": tool_exit_code(event),
            }))
        })
        .collect()
}

fn score_diff_applies(events: &[Value]) -> ScoreDraft {
    let patch_events = events
        .iter()
        .filter(|event| {
            matches!(
                event.get("event_type").and_then(Value::as_str),
                Some("post_tool" | "tool_failure")
            ) && event_looks_like_patch_application(event)
        })
        .map(|event| {
            let tool = event.get("tool").unwrap_or(&Value::Null);
            json!({
                "event_id": event.get("event_id").cloned().unwrap_or(Value::Null),
                "event_type": event.get("event_type").cloned().unwrap_or(Value::Null),
                "tool": tool.get("name").cloned().unwrap_or(Value::Null),
                "command": extract_tool_command(event),
                "exit_code": tool_exit_code(event),
            })
        })
        .collect::<Vec<_>>();
    if patch_events.is_empty() {
        return ScoreDraft {
            scorer: "diff_applies".to_string(),
            value: None,
            passed: true,
            detail: json!({"status": "not_applicable", "reason": "no_patch_or_edit_event_captured", "patch_events": []}),
        };
    }
    let failures = patch_events
        .iter()
        .filter(|event| {
            event["event_type"] == "tool_failure"
                || event
                    .get("exit_code")
                    .and_then(Value::as_i64)
                    .is_some_and(|code| code != 0)
        })
        .cloned()
        .collect::<Vec<_>>();
    let passed = failures.is_empty();
    ScoreDraft {
        scorer: "diff_applies".to_string(),
        value: if passed { Some(1.0) } else { Some(0.0) },
        passed,
        detail: json!({"status": if passed { "passed" } else { "failed" }, "patch_events": patch_events, "failures": failures}),
    }
}

fn score_outcome_judge(
    fixture: &FixtureRecord,
    run: Option<&crate::storage::RunSummary>,
    events: &[Value],
    judge_command: Option<&str>,
) -> Result<ScoreDraft, String> {
    let Some(judge_command) = judge_command.filter(|command| !command.trim().is_empty()) else {
        return Ok(ScoreDraft {
            scorer: "outcome_judge".to_string(),
            value: None,
            passed: false,
            detail: json!({
                "status": "unconfigured",
                "reason": "outcome_judge requires judge_command in config.json, TRANQUIL_JUDGE_COMMAND, or --judge-command",
            }),
        });
    };
    let payload = json!({
        "fixture_id": fixture.fixture_id,
        "suite": fixture.suite,
        "prompt": fixture.prompt,
        "rubric": fixture.repo_ref.get("rubric").cloned().unwrap_or(Value::Null),
        "reference": fixture.repo_ref.get("reference").cloned().unwrap_or(Value::Null),
        "repo_ref": fixture.repo_ref,
        "run": run,
        "events": events,
    });
    let completed = run_shell_command(
        judge_command,
        None,
        &[],
        Some(&serde_json::to_string(&payload).map_err(|err| err.to_string())?),
    )?;
    if completed.status != 0 {
        return Ok(ScoreDraft {
            scorer: "outcome_judge".to_string(),
            value: Some(completed.status as f64),
            passed: false,
            detail: json!({
                "status": "judge_command_failed",
                "returncode": completed.status,
                "stderr": tail(&completed.stderr, 2000),
                "stdout": tail(&completed.stdout, 2000),
            }),
        });
    }
    let parsed: Value = serde_json::from_str(completed.stdout.trim()).unwrap_or_else(|err| {
        json!({
            "_invalid_json": true,
            "error": err.to_string(),
            "stdout": tail(&completed.stdout, 2000),
        })
    });
    let Some(mut detail) = parsed.as_object().cloned() else {
        return Ok(ScoreDraft {
            scorer: "outcome_judge".to_string(),
            value: None,
            passed: false,
            detail: json!({"status": "invalid_judge_json", "reason": "judge output must be a JSON object"}),
        });
    };
    if detail
        .get("_invalid_json")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        detail.insert("status".to_string(), json!("invalid_judge_json"));
        return Ok(ScoreDraft {
            scorer: "outcome_judge".to_string(),
            value: None,
            passed: false,
            detail: Value::Object(detail),
        });
    }
    let passed = detail
        .get("passed")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let value = detail
        .get("score")
        .or_else(|| detail.get("value"))
        .and_then(Value::as_f64)
        .or(Some(if passed { 1.0 } else { 0.0 }));
    detail
        .entry("status".to_string())
        .or_insert_with(|| json!(if passed { "passed" } else { "failed" }));
    detail.insert(
        "judge_command".to_string(),
        json!(judge_command.split_whitespace().next().unwrap_or("")),
    );
    Ok(ScoreDraft {
        scorer: "outcome_judge".to_string(),
        value,
        passed,
        detail: Value::Object(detail),
    })
}

fn resolve_baseline_eval_run(
    storage: &Storage,
    suite: &str,
    baseline: Option<&str>,
) -> Result<Option<String>, String> {
    let Some(baseline) = baseline.filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    if baseline == "last-green" {
        return Ok(storage
            .latest_eval_run(suite, Some("passed"))?
            .map(|run| run.eval_run_id));
    }
    Ok(Some(baseline.to_string()))
}

fn compare_to_baseline(
    storage: &Storage,
    eval_run_id: &str,
    baseline_eval_run_id: &str,
) -> Result<Vec<RegressionDraft>, String> {
    let baseline_scores = storage.list_scores(baseline_eval_run_id)?;
    let current_scores = storage.list_scores(eval_run_id)?;
    let mut current = HashMap::new();
    for score in current_scores {
        current.insert((score.fixture_id.clone(), score.scorer.clone()), score);
    }
    let mut regressions = Vec::new();
    for baseline_score in baseline_scores.into_iter().filter(|score| score.passed) {
        let key = (
            baseline_score.fixture_id.clone(),
            baseline_score.scorer.clone(),
        );
        if current.get(&key).is_some_and(|score| !score.passed) {
            regressions.push(RegressionDraft {
                scorer: "regression".to_string(),
                fixture_id: baseline_score.fixture_id.clone(),
                value: Some(1.0),
                passed: false,
                detail: json!({
                    "baseline_eval_run_id": baseline_eval_run_id,
                    "regressed_scorer": baseline_score.scorer,
                    "baseline_value": baseline_score.value,
                    "current_value": current.get(&key).and_then(|score| score.value),
                }),
            });
        }
    }
    Ok(regressions)
}

fn repeated_tool_inputs(events: &[Value]) -> HashMap<String, i64> {
    let mut counts = HashMap::new();
    for event in events {
        let tool = event.get("tool").unwrap_or(&Value::Null);
        if tool.is_null() {
            continue;
        }
        let key = format!(
            "{}:{}",
            tool.get("name")
                .and_then(Value::as_str)
                .unwrap_or("unknown"),
            serde_json::to_string(tool.get("input").unwrap_or(&Value::Null))
                .unwrap_or_else(|_| "null".to_string())
        );
        *counts.entry(key).or_default() += 1;
    }
    counts.retain(|_, count| *count > 1);
    counts
}

fn event_has_check(event: &Value) -> bool {
    extract_tool_command(event).is_some_and(|command| command_looks_like_check(&command))
}

fn event_looks_like_patch_application(event: &Value) -> bool {
    let tool = event.get("tool").unwrap_or(&Value::Null);
    let name = tool
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    if [
        "apply_patch",
        "edit",
        "multiedit",
        "write",
        "notebookedit",
        "str_replace_editor",
    ]
    .contains(&name.as_str())
    {
        return true;
    }
    extract_tool_command(event).is_some_and(|command| {
        let lowered = command.to_ascii_lowercase();
        ["apply_patch", "git apply", "patch -p", "patch <"]
            .iter()
            .any(|term| lowered.contains(term))
    })
}

fn tool_exit_code(event: &Value) -> Option<i64> {
    let tool = event.get("tool").unwrap_or(&Value::Null);
    let output = tool.get("output").unwrap_or(&Value::Null);
    for source in [output, event.get("raw").unwrap_or(&Value::Null)] {
        for key in ["exit_code", "returncode", "return_code"] {
            if let Some(code) = source.get(key).and_then(Value::as_i64) {
                return Some(code);
            }
        }
    }
    None
}

fn run_wall_clock_s(run: &crate::storage::RunSummary) -> Option<f64> {
    let started = parse_timestamp_number(&run.started_at)?;
    let ended = run
        .ended_at
        .as_deref()
        .and_then(parse_timestamp_number)
        .or_else(|| parse_timestamp_number(&run.last_event_at))?;
    Some(((ended - started) / 1000.0).max(0.0))
}

fn parse_timestamp_number(value: &str) -> Option<f64> {
    value.parse::<f64>().ok()
}

fn wildcard_match(pattern: &str, value: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    if !pattern.contains('*') {
        return pattern == value;
    }
    let mut remaining = value;
    let starts_with_wildcard = pattern.starts_with('*');
    let ends_with_wildcard = pattern.ends_with('*');
    let parts: Vec<&str> = pattern.split('*').filter(|part| !part.is_empty()).collect();
    if parts.is_empty() {
        return true;
    }
    if !starts_with_wildcard {
        let first = parts[0];
        if !remaining.starts_with(first) {
            return false;
        }
        remaining = &remaining[first.len()..];
    }
    for (index, part) in parts.iter().enumerate() {
        if index == 0 && !starts_with_wildcard {
            continue;
        }
        let Some(offset) = remaining.find(part) else {
            return false;
        };
        remaining = &remaining[offset + part.len()..];
    }
    if !ends_with_wildcard {
        if let Some(last) = parts.last() {
            return value.ends_with(last);
        }
    }
    true
}

fn tail(value: &str, max_chars: usize) -> String {
    let length = value.chars().count();
    if length <= max_chars {
        value.to_string()
    } else {
        value.chars().skip(length - max_chars).collect()
    }
}

fn command_looks_like_check(command: &str) -> bool {
    command_looks_like_test(command)
        || command_looks_like_build(command)
        || contains_any(
            command,
            &[
                "ruff",
                "eslint",
                "flake8",
                "pylint",
                "npm run lint",
                "pnpm lint",
                "pnpm run lint",
                "yarn lint",
                "yarn run lint",
                "go vet",
            ],
        )
}

fn command_looks_like_test(command: &str) -> bool {
    contains_any(
        command,
        &[
            "pytest",
            "unittest",
            "vitest",
            "jest",
            "cargo test",
            "go test",
            "make test",
            "npm test",
            "npm run test",
            "pnpm test",
            "pnpm run test",
            "yarn test",
            "yarn run test",
            "bun test",
            "bun run test",
            "tox",
            "nox",
            "rspec",
            "phpunit",
            "mvn test",
            "gradle test",
            "./gradlew test",
        ],
    )
}

fn command_looks_like_build(command: &str) -> bool {
    contains_any(
        command,
        &[
            "npm run build",
            "pnpm build",
            "pnpm run build",
            "yarn build",
            "yarn run build",
            "bun run build",
            "cargo build",
            "go build",
            "make build",
            "cmake --build",
            "mvn package",
            "gradle build",
            "./gradlew build",
            "typecheck",
            "tsc",
            "mypy",
        ],
    )
}

fn contains_any(command: &str, terms: &[&str]) -> bool {
    let lowered = command.to_ascii_lowercase();
    terms.iter().any(|term| lowered.contains(term))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::normalize::normalize_event;

    #[test]
    fn eval_scores_fixture_and_replay_command() {
        let root =
            std::env::temp_dir().join(format!("tranquil-rust-evals-{}", crate::util::now_millis()));
        let storage = Storage::open(&root.join("tranquil.db")).unwrap();
        let prompt = normalize_event(
            "UserPromptSubmit",
            &json!({"session_id": "eval", "prompt": "ship"}),
            "hook",
        );
        let test_event = normalize_event(
            "PostToolUse",
            &json!({
                "session_id": "eval",
                "tool_name": "Bash",
                "tool_input": {"command": "cargo test"},
                "tool_output": {"exit_code": 0}
            }),
            "hook",
        );
        storage.record_event(&prompt).unwrap();
        storage.record_event(&test_event).unwrap();
        let fixture = storage
            .create_fixture(
                prompt["run_id"].as_str().unwrap(),
                "default",
                Some("fix_eval"),
            )
            .unwrap();
        assert_eq!(fixture.prompt.as_deref(), Some("ship"));
        let (_eval_run_id, scores) =
            run_eval(&storage, "default", &["tests_pass".to_string()], None, None).unwrap();
        assert_eq!(scores.len(), 1);
        assert!(scores[0].passed);
        let replay_command = if cfg!(windows) {
            "Test-Path $env:TRANQUIL_PROMPT_FILE"
        } else {
            "test -f \"$TRANQUIL_PROMPT_FILE\""
        };
        let (_replay_id, replay_scores) =
            replay_fixture(&storage, "fix_eval", replay_command, &root.join("replays")).unwrap();
        assert!(
            replay_scores
                .iter()
                .any(|score| score.scorer == "replay_command_exits_zero" && score.passed)
        );
        let _ = std::fs::remove_dir_all(root);
    }
}
