from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .storage import Storage


def load_suite_file(path: str | Path) -> dict[str, Any]:
    data = parse_simple_yaml(Path(path).expanduser().read_text(encoding="utf-8"))
    if "suite" not in data:
        raise ValueError("suite file must define 'suite'")
    fixtures = data.get("fixtures", [])
    if isinstance(fixtures, str):
        fixtures = [fixtures]
    if not isinstance(fixtures, list):
        raise ValueError("suite fixtures must be a list")
    matrix = data.get("matrix", [])
    if matrix is None:
        matrix = []
    if not isinstance(matrix, list):
        raise ValueError("suite matrix must be a list")
    scorers = data.get("scorers", [])
    if scorers is None:
        scorers = []
    if isinstance(scorers, str):
        scorers = [scorers]
    if not isinstance(scorers, list):
        raise ValueError("suite scorers must be a list")
    data["fixtures"] = fixtures
    data["matrix"] = matrix
    data["scorers"] = [str(scorer) for scorer in scorers]
    return data


def load_fixture_file(path: str | Path) -> dict[str, Any]:
    data = parse_simple_yaml(Path(path).expanduser().read_text(encoding="utf-8"))
    if "fixture" not in data:
        raise ValueError("fixture file must define 'fixture'")
    if "from_run" not in data:
        raise ValueError("fixture file must define 'from_run'")
    return data


def import_fixture_file(storage: Storage, path: str | Path, suite: str | None = None) -> dict[str, Any]:
    data = load_fixture_file(path)
    return storage.upsert_fixture_definition(
        fixture_id=str(data["fixture"]),
        run_id=str(data["from_run"]),
        suite=suite or str(data.get("suite") or "default"),
        prompt=data.get("prompt"),
        repo_ref=data.get("repo_ref") if isinstance(data.get("repo_ref"), dict) else None,
        budgets=data.get("budgets") if isinstance(data.get("budgets"), dict) else None,
        forbidden_paths=data.get("forbidden_paths") if isinstance(data.get("forbidden_paths"), list) else None,
        rubric=data.get("rubric") if isinstance(data.get("rubric"), str) else None,
        reference=data.get("reference") if isinstance(data.get("reference"), str) else None,
    )


def import_suite_fixtures(storage: Storage, suite_path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    suite_path = Path(suite_path).expanduser()
    suite = load_suite_file(suite_path)
    imported = []
    for item in suite["fixtures"]:
        if isinstance(item, dict):
            fixture_data = item
        else:
            fixture_file = resolve_fixture_path(suite_path, str(item))
            if fixture_file.exists():
                fixture_data = load_fixture_file(fixture_file)
            else:
                continue
        imported.append(
            storage.upsert_fixture_definition(
                fixture_id=str(fixture_data["fixture"]),
                run_id=str(fixture_data["from_run"]),
                suite=str(suite["suite"]),
                prompt=fixture_data.get("prompt"),
                repo_ref=fixture_data.get("repo_ref") if isinstance(fixture_data.get("repo_ref"), dict) else None,
                budgets=fixture_data.get("budgets") if isinstance(fixture_data.get("budgets"), dict) else None,
                forbidden_paths=fixture_data.get("forbidden_paths") if isinstance(fixture_data.get("forbidden_paths"), list) else None,
                rubric=fixture_data.get("rubric") if isinstance(fixture_data.get("rubric"), str) else None,
                reference=fixture_data.get("reference") if isinstance(fixture_data.get("reference"), str) else None,
            )
        )
    return suite, imported


def resolve_fixture_path(suite_path: Path, name: str) -> Path:
    candidate = Path(name)
    if candidate.is_absolute():
        return candidate
    if candidate.suffix in {".yaml", ".yml"}:
        return suite_path.parent / candidate
    return suite_path.parent.parent / "fixtures" / f"{name}.yaml"


def parse_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[Any] | None = None
    for raw_line in text.splitlines():
        line = strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key and current_list is not None:
            current_list.append(parse_value(line[4:].strip()))
            continue
        if line.startswith("  ") and current_key and isinstance(result.get(current_key), dict):
            key, value = split_key_value(line.strip())
            result[current_key][key] = parse_value(value)
            continue
        key, value = split_key_value(line.strip())
        if value == "":
            result[key] = []
            current_key = key
            current_list = result[key]
        else:
            result[key] = parse_value(value)
            current_key = None
            current_list = None
    return result


def split_key_value(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise ValueError(f"invalid YAML line: {line}")
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def strip_comment(line: str) -> str:
    in_quote: str | None = None
    for index, char in enumerate(line):
        if char in {"'", '"'}:
            in_quote = None if in_quote == char else char if in_quote is None else in_quote
        if char == "#" and in_quote is None:
            return line[:index]
    return line


def parse_value(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "~"}:
        return None
    if value.startswith("{") and value.endswith("}"):
        return parse_inline_map(value)
    if value.startswith("[") and value.endswith("]"):
        return parse_inline_list(value)
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return ast.literal_eval(value)
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_inline_list(value: str) -> list[Any]:
    inner = value[1:-1].strip()
    if not inner:
        return []
    return [parse_value(part.strip()) for part in split_top_level(inner)]


def parse_inline_map(value: str) -> dict[str, Any]:
    inner = value[1:-1].strip()
    if not inner:
        return {}
    result = {}
    for part in split_top_level(inner):
        key, raw_value = split_key_value(part)
        result[key] = parse_value(raw_value)
    return result


def split_top_level(value: str) -> list[str]:
    parts = []
    start = 0
    depth = 0
    quote: str | None = None
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
        elif quote is None:
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(value[start:index])
                start = index + 1
    parts.append(value[start:])
    return parts
