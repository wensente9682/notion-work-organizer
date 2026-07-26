#!/usr/bin/env python3
"""
Local-only setup/adopt preflight for the Notion work maintenance workflow.

This script validates local JSON config and optional local schema fixtures. It
does not call Notion, require tokens, create databases, modify fields, write
rows, create ledgers, or change the organize runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_FIELD_MAPPING = {
    "task": "Name",
    "done": "完成",
    "category": "category",
    "takeaway": "收获",
    "improvement": "改进",
}
PRIVATE_KEY_PARTS = ("token", "secret", "credential", "password", "api_key")
PLACEHOLDER_PREFIXES = ("YOUR_", "REPLACE_", "TODO_")


@dataclass
class Finding:
    level: str
    code: str
    message: str


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise SystemExit(f"error: file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"error: expected JSON object in {path}")
    return data


def is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PLACEHOLDER_PREFIXES)


def property_type(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        type_value = value.get("type")
        if isinstance(type_value, str):
            return type_value
    return None


def field_mapping(config: dict[str, Any] | None = None) -> dict[str, str]:
    mapping = dict(DEFAULT_FIELD_MAPPING)
    custom = (config or {}).get("field_mapping") or {}
    if isinstance(custom, dict):
        mapping.update({str(key): str(value) for key, value in custom.items() if value})
    return mapping


def required_source_types(config: dict[str, Any] | None = None) -> dict[str, set[str]]:
    fields = field_mapping(config)
    return {
        fields["task"]: {"title"},
        fields["done"]: {"checkbox"},
        fields["category"]: {"rich_text", "relation", "select", "multi_select", "status"},
        fields["takeaway"]: {"rich_text"},
        fields["improvement"]: {"rich_text"},
    }


def required_archive_types(config: dict[str, Any] | None = None) -> dict[str, set[str]]:
    fields = field_mapping(config)
    return {
        fields["task"]: {"title"},
        fields["takeaway"]: {"rich_text"},
        fields["improvement"]: {"rich_text"},
    }


def find_private_keys(data: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            key_path = f"{prefix}.{key}" if prefix else str(key)
            lowered = str(key).lower()
            if any(part in lowered for part in PRIVATE_KEY_PARTS):
                paths.append(key_path)
            paths.extend(find_private_keys(value, key_path))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            paths.extend(find_private_keys(value, f"{prefix}[{index}]"))
    return paths


def find_placeholders(data: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            key_path = f"{prefix}.{key}" if prefix else str(key)
            if is_placeholder(value):
                paths.append(key_path)
            paths.extend(find_placeholders(value, key_path))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            item_path = f"{prefix}[{index}]"
            if is_placeholder(value):
                paths.append(item_path)
            paths.extend(find_placeholders(value, item_path))
    return paths


def validate_config(config: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    mode = config.get("mode", "test")
    if mode not in {"test", "real"}:
        findings.append(Finding("error", "config.mode", "mode must be omitted, 'test', or 'real'."))

    for path in find_private_keys(config):
        findings.append(Finding("error", "config.private-key", f"private credential-like key is not allowed: {path}"))

    for field in ("source_database_id", "source_order", "batch_size", "move_limit"):
        if field not in config:
            findings.append(Finding("error", "config.missing", f"missing required config field: {field}"))

    if config.get("source_order") not in {None, "bottom_first"}:
        findings.append(Finding("error", "config.source-order", "source_order must be 'bottom_first'."))

    for field in ("batch_size", "move_limit"):
        if field in config and (not isinstance(config[field], int) or config[field] <= 0):
            findings.append(Finding("error", "config.number", f"{field} must be a positive integer."))

    if mode == "real":
        for field in ("archive_tables_page_id", "archive_tables"):
            if field not in config:
                findings.append(Finding("error", "config.missing", f"missing required real config field: {field}"))
        archive_tables = config.get("archive_tables")
        project_categories = config.get("project_categories")
        if isinstance(archive_tables, dict) and isinstance(project_categories, dict):
            archive_keys = set(archive_tables)
            project_keys = set(project_categories)
            for category in sorted(project_keys - archive_keys):
                findings.append(Finding("error", "config.mapping", f"category lacks archive_tables mapping: {category}"))
            for category in sorted(archive_keys - project_keys):
                findings.append(Finding("error", "config.mapping", f"category lacks project_categories mapping: {category}"))
    else:
        targets = config.get("target_databases")
        if not isinstance(targets, dict) or not targets:
            findings.append(Finding("error", "config.target-databases", "target_databases must map categories to archive database IDs."))

    for path in find_placeholders(config):
        findings.append(Finding("warning", "config.placeholder", f"placeholder value still needs local replacement: {path}"))

    return findings


def properties_from_schema_section(section: Any) -> dict[str, Any]:
    if isinstance(section, dict) and isinstance(section.get("properties"), dict):
        return section["properties"]
    if isinstance(section, dict):
        return section
    return {}


def archive_sections(schema: dict[str, Any]) -> dict[str, Any]:
    archives = schema.get("archives")
    if isinstance(archives, dict):
        return archives
    archive_tables = schema.get("archive_tables")
    if isinstance(archive_tables, dict):
        return archive_tables
    archive = schema.get("archive")
    if isinstance(archive, dict):
        return {"archive": archive}
    return {}


def validate_properties(
    properties: dict[str, Any],
    required: dict[str, set[str]],
    *,
    scope: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for name, allowed_types in required.items():
        if name not in properties:
            findings.append(Finding("error", "schema.missing-field", f"{scope} is missing required property: {name}"))
            continue
        found_type = property_type(properties[name])
        if found_type not in allowed_types:
            allowed = ", ".join(sorted(allowed_types))
            findings.append(
                Finding(
                    "error",
                    "schema.incompatible-type",
                    f"{scope}.{name} has type {found_type or 'unknown'}; expected {allowed}.",
                )
            )
    return findings


def validate_schema(schema: dict[str, Any], config: dict[str, Any] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    source = schema.get("source")
    if source is None:
        source = schema.get("source_database")
    source_properties = properties_from_schema_section(source)
    if not source_properties:
        findings.append(Finding("error", "schema.source", "schema must include source.properties."))
    else:
        findings.extend(validate_properties(source_properties, required_source_types(config), scope="source"))
        fields = field_mapping(config)
        category_type = property_type(source_properties.get(fields["category"]))
        if (config or {}).get("mode") == "real" and category_type == "relation":
            project_categories = (config or {}).get("project_categories")
            if not isinstance(project_categories, dict) or not project_categories:
                findings.append(
                    Finding(
                        "error",
                        "config.mapping",
                        "relation Category requires project_categories mapping.",
                    )
                )

    archives = archive_sections(schema)
    if not archives:
        findings.append(Finding("error", "schema.archive", "schema must include archives or archive_tables."))
    for name, section in archives.items():
        archive_properties = properties_from_schema_section(section)
        if not archive_properties:
            findings.append(Finding("error", "schema.archive", f"archive {name} must include properties."))
            continue
        findings.extend(validate_properties(archive_properties, required_archive_types(config), scope=f"archive[{name}]"))

    return findings


def status_from_findings(findings: list[Finding]) -> str:
    if any(finding.level == "error" for finding in findings):
        return "blocked"
    if any(finding.level == "warning" for finding in findings):
        return "needs local edits"
    return "ready"


def format_report(config_path: Path, schema_path: Path | None, findings: list[Finding]) -> str:
    lines = [
        "Setup/adopt preflight: local-only read-only",
        f"config: {config_path}",
        f"schema: {schema_path if schema_path else 'not provided'}",
        f"status: {status_from_findings(findings)}",
    ]
    if findings:
        lines.append("")
        lines.append("Findings:")
        for finding in findings:
            lines.append(f"- [{finding.level}] {finding.code}: {finding.message}")
    else:
        lines.append("")
        lines.append("No findings.")
    lines.append("")
    lines.append("Not checked: live Notion access, real database IDs, remote schemas, row contents, permissions.")
    lines.append("Not performed: database/field/archive-table/row/ledger/safety-log creation or mutation.")
    return "\n".join(lines)


def run(config_path: Path, schema_path: Path | None = None) -> tuple[str, int]:
    config = load_json(config_path)
    findings = validate_config(config)
    if schema_path:
        schema = load_json(schema_path)
        findings.extend(validate_schema(schema, config))
    report = format_report(config_path, schema_path, findings)
    exit_code = 1 if any(finding.level == "error" for finding in findings) else 0
    return report, exit_code


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local-only setup/adopt preflight for Notion work maintenance configs.")
    parser.add_argument("--config", required=True, type=Path, help="Local config JSON to inspect.")
    parser.add_argument("--schema", type=Path, help="Optional local schema JSON fixture to inspect.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report, exit_code = run(args.config, args.schema)
    print(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
