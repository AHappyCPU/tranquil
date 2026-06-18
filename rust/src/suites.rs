use std::path::{Path, PathBuf};

use serde_json::{Value, json};

use crate::storage::{FixtureRecord, Storage};

#[derive(Debug, Clone)]
pub struct SuiteDefinition {
    pub suite: String,
    pub fixtures: Vec<Value>,
    pub scorers: Vec<String>,
    pub matrix: Vec<Value>,
    pub baseline: Option<String>,
}

pub fn load_suite_file(path: &Path) -> Result<SuiteDefinition, String> {
    let data = parse_simple_yaml(&std::fs::read_to_string(path).map_err(|err| err.to_string())?)?;
    let suite = data
        .get("suite")
        .and_then(Value::as_str)
        .ok_or_else(|| "suite file must define 'suite'".to_string())?
        .to_string();
    let fixtures = list_value(data.get("fixtures"));
    let scorers = list_value(data.get("scorers"))
        .into_iter()
        .filter_map(|value| value.as_str().map(ToString::to_string))
        .collect::<Vec<_>>();
    let matrix = list_value(data.get("matrix"));
    let baseline = data
        .get("baseline")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    Ok(SuiteDefinition {
        suite,
        fixtures,
        scorers,
        matrix,
        baseline,
    })
}

pub fn import_fixture_file(
    storage: &Storage,
    path: &Path,
    suite_override: Option<&str>,
) -> Result<FixtureRecord, String> {
    let data = load_fixture_file(path)?;
    upsert_fixture_from_data(
        storage,
        &data,
        suite_override
            .or_else(|| data.get("suite").and_then(Value::as_str))
            .unwrap_or("default"),
    )
}

pub fn import_suite_fixtures(
    storage: &Storage,
    suite_path: &Path,
) -> Result<(SuiteDefinition, Vec<FixtureRecord>), String> {
    let suite = load_suite_file(suite_path)?;
    let mut imported = Vec::new();
    for item in &suite.fixtures {
        let fixture_data = if item.is_object() {
            item.clone()
        } else if let Some(name) = item.as_str() {
            let fixture_path = resolve_fixture_path(suite_path, name);
            if !fixture_path.exists() {
                continue;
            }
            load_fixture_file(&fixture_path)?
        } else {
            continue;
        };
        imported.push(upsert_fixture_from_data(
            storage,
            &fixture_data,
            &suite.suite,
        )?);
    }
    Ok((suite, imported))
}

fn load_fixture_file(path: &Path) -> Result<Value, String> {
    let data = parse_simple_yaml(&std::fs::read_to_string(path).map_err(|err| err.to_string())?)?;
    if data.get("fixture").is_none() {
        return Err("fixture file must define 'fixture'".to_string());
    }
    if data.get("from_run").is_none() {
        return Err("fixture file must define 'from_run'".to_string());
    }
    Ok(Value::Object(data))
}

fn upsert_fixture_from_data(
    storage: &Storage,
    data: &Value,
    suite: &str,
) -> Result<FixtureRecord, String> {
    let fixture_id = data
        .get("fixture")
        .and_then(Value::as_str)
        .ok_or_else(|| "fixture file must define 'fixture'".to_string())?;
    let run_id = data
        .get("from_run")
        .and_then(Value::as_str)
        .ok_or_else(|| "fixture file must define 'from_run'".to_string())?;
    let forbidden_paths = data
        .get("forbidden_paths")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(ToString::to_string))
                .collect::<Vec<_>>()
        });
    storage.upsert_fixture_definition(
        fixture_id,
        run_id,
        suite,
        data.get("prompt").and_then(Value::as_str),
        data.get("repo_ref")
            .filter(|value| value.is_object())
            .cloned(),
        data.get("budgets")
            .filter(|value| value.is_object())
            .cloned(),
        forbidden_paths,
        data.get("rubric").and_then(Value::as_str),
        data.get("reference").and_then(Value::as_str),
    )
}

fn resolve_fixture_path(suite_path: &Path, name: &str) -> PathBuf {
    let candidate = PathBuf::from(name);
    if candidate.is_absolute() {
        return candidate;
    }
    if matches!(
        candidate.extension().and_then(|value| value.to_str()),
        Some("yaml" | "yml")
    ) {
        return suite_path
            .parent()
            .unwrap_or(Path::new("."))
            .join(candidate);
    }
    suite_path
        .parent()
        .and_then(Path::parent)
        .unwrap_or(Path::new("."))
        .join("fixtures")
        .join(format!("{name}.yaml"))
}

fn parse_simple_yaml(text: &str) -> Result<serde_json::Map<String, Value>, String> {
    let mut result = serde_json::Map::new();
    let mut current_key: Option<String> = None;
    for raw_line in text.lines() {
        let line = strip_comment(raw_line)
            .trim_start_matches('\u{feff}')
            .trim_end()
            .to_string();
        if line.trim().is_empty() {
            continue;
        }
        if let Some(key) = current_key.clone() {
            if line.starts_with("  - ") {
                let item = parse_value(line[4..].trim())?;
                result
                    .entry(key)
                    .or_insert_with(|| Value::Array(Vec::new()))
                    .as_array_mut()
                    .ok_or_else(|| "YAML list state corrupted".to_string())?
                    .push(item);
                continue;
            }
            if line.starts_with("  ") && result.get(&key).is_some_and(Value::is_object) {
                let (child_key, value) = split_key_value(line.trim())?;
                result
                    .get_mut(&key)
                    .and_then(Value::as_object_mut)
                    .ok_or_else(|| "YAML map state corrupted".to_string())?
                    .insert(child_key, parse_value(value)?);
                continue;
            }
        }
        let (key, value) = split_key_value(line.trim())?;
        if value.is_empty() {
            result.insert(key.clone(), Value::Array(Vec::new()));
            current_key = Some(key);
        } else {
            result.insert(key, parse_value(value)?);
            current_key = None;
        }
    }
    Ok(result)
}

fn split_key_value(line: &str) -> Result<(String, &str), String> {
    let Some((key, value)) = line.split_once(':') else {
        return Err(format!("invalid YAML line: {line}"));
    };
    Ok((key.trim().to_string(), value.trim()))
}

fn strip_comment(line: &str) -> String {
    let mut quote: Option<char> = None;
    for (index, ch) in line.char_indices() {
        if matches!(ch, '\'' | '"') {
            quote = if quote == Some(ch) {
                None
            } else if quote.is_none() {
                Some(ch)
            } else {
                quote
            };
        }
        if ch == '#' && quote.is_none() {
            return line[..index].to_string();
        }
    }
    line.to_string()
}

fn parse_value(value: &str) -> Result<Value, String> {
    let value = value.trim();
    if value.is_empty() {
        return Ok(Value::String(String::new()));
    }
    if value == "true" || value == "false" {
        return Ok(Value::Bool(value == "true"));
    }
    if value == "null" || value == "~" {
        return Ok(Value::Null);
    }
    if value.starts_with('{') && value.ends_with('}') {
        return parse_inline_map(value);
    }
    if value.starts_with('[') && value.ends_with(']') {
        return parse_inline_list(value);
    }
    if (value.starts_with('"') && value.ends_with('"'))
        || (value.starts_with('\'') && value.ends_with('\''))
    {
        return Ok(Value::String(unquote(value)));
    }
    if let Ok(number) = value.parse::<i64>() {
        return Ok(json!(number));
    }
    if let Ok(number) = value.parse::<f64>() {
        return Ok(json!(number));
    }
    Ok(Value::String(value.to_string()))
}

fn parse_inline_list(value: &str) -> Result<Value, String> {
    let inner = value[1..value.len() - 1].trim();
    if inner.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }
    Ok(Value::Array(
        split_top_level(inner)
            .into_iter()
            .map(|part| parse_value(part.trim()))
            .collect::<Result<Vec<_>, _>>()?,
    ))
}

fn parse_inline_map(value: &str) -> Result<Value, String> {
    let inner = value[1..value.len() - 1].trim();
    let mut result = serde_json::Map::new();
    if inner.is_empty() {
        return Ok(Value::Object(result));
    }
    for part in split_top_level(inner) {
        let (key, value) = split_key_value(part.trim())?;
        result.insert(key, parse_value(value)?);
    }
    Ok(Value::Object(result))
}

fn split_top_level(value: &str) -> Vec<&str> {
    let mut parts = Vec::new();
    let mut start = 0;
    let mut depth = 0_i64;
    let mut quote: Option<char> = None;
    for (index, ch) in value.char_indices() {
        if matches!(ch, '\'' | '"') {
            quote = if quote == Some(ch) {
                None
            } else if quote.is_none() {
                Some(ch)
            } else {
                quote
            };
        } else if quote.is_none() {
            if matches!(ch, '[' | '{') {
                depth += 1;
            } else if matches!(ch, ']' | '}') {
                depth -= 1;
            } else if ch == ',' && depth == 0 {
                parts.push(&value[start..index]);
                start = index + 1;
            }
        }
    }
    parts.push(&value[start..]);
    parts
}

fn unquote(value: &str) -> String {
    value[1..value.len() - 1]
        .replace("\\\"", "\"")
        .replace("\\'", "'")
}

fn list_value(value: Option<&Value>) -> Vec<Value> {
    match value {
        Some(Value::Array(items)) => items.clone(),
        Some(Value::String(text)) => vec![Value::String(text.clone())],
        Some(value) if !value.is_null() => vec![value.clone()],
        _ => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::normalize::normalize_event;

    #[test]
    fn imports_fixture_and_suite_yaml() {
        let root =
            std::env::temp_dir().join(format!("tranquil-rust-suite-{}", crate::util::now_millis()));
        let fixtures_dir = root.join("tranquil").join("fixtures");
        let suites_dir = root.join("tranquil").join("suites");
        std::fs::create_dir_all(&fixtures_dir).unwrap();
        std::fs::create_dir_all(&suites_dir).unwrap();
        let storage = Storage::open(&root.join("tranquil.db")).unwrap();
        let event = normalize_event(
            "UserPromptSubmit",
            &json!({"session_id": "suite", "prompt": "fix auth"}),
            "hook",
        );
        storage.record_event(&event).unwrap();
        std::fs::write(
            fixtures_dir.join("refactor-auth.yaml"),
            format!(
                "fixture: refactor-auth\nfrom_run: {}\nrubric: Keep auth behavior safe.\nbudgets: {{cost_usd: 1.5}}\n",
                event["run_id"].as_str().unwrap()
            ),
        )
        .unwrap();
        let suite_path = suites_dir.join("refactor.yaml");
        std::fs::write(
            &suite_path,
            "suite: refactor\nfixtures: [refactor-auth]\nscorers: [tests_pass]\nbaseline: last-green\nmatrix:\n  - {name: prompt-file, command: \"echo ok\"}\n",
        )
        .unwrap();
        let (suite, imported) = import_suite_fixtures(&storage, &suite_path).unwrap();
        assert_eq!(suite.suite, "refactor");
        assert_eq!(suite.scorers, vec!["tests_pass"]);
        assert_eq!(suite.baseline.as_deref(), Some("last-green"));
        assert_eq!(suite.matrix.len(), 1);
        assert_eq!(imported.len(), 1);
        assert_eq!(imported[0].fixture_id, "refactor-auth");
        assert_eq!(imported[0].repo_ref["budgets"]["cost_usd"], 1.5);
        let _ = std::fs::remove_dir_all(root);
    }
}
