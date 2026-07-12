#!/usr/bin/env python3
"""
External CLI fallback for the approval-first Notion to-do organizer.

Normal Codex operation should use the todo-archive-review skill with the
Notion connector. This script talks directly to the public Notion API and is
intended only for outside-Codex command-line use.

Audience: this file is developer-facing core plus an advanced-user fallback.
It is kept as a regression-tested surface for maintainers, local debugging, and
external CLI operation. Ordinary Codex use should go through the skill and
Notion connector instead.

The script is intentionally conservative:
- it only touches databases listed in the config file;
- it stages at most 5 candidates at a time;
- it stages from the bottom/oldest part of the source table first;
- it moves only rows explicitly approved with `ok`;
- undo archives only pages created by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from difflib import get_close_matches
from pathlib import Path
from typing import Any


NOTION_VERSION = "2022-06-28"
DEFAULT_STATE = ".todo_archive/state.json"
DEFAULT_CACHE = ".todo_archive/cache.json"
REAL_MODE = "real"
TEST_MODE = "test"
PREFLIGHT_TTL_SECONDS = 30 * 60
LEDGER_CACHE_TTL_SECONDS = 10 * 60
ARCHIVE_TARGET_CACHE_TTL_SECONDS = 30 * 60
SCRIPT_DIR = Path(__file__).resolve().parent
BACKUP_HELPER = SCRIPT_DIR / "skills/todo-archive-review/scripts/todo_archive_backup.py"
DEFAULT_FIELD_MAPPING = {
    "task": "Name",
    "done": "完成",
    "category": "category",
    "takeaway": "收获",
    "improvement": "改进",
}


class WorkflowError(Exception):
    pass


def field_mapping(config: dict[str, Any] | None = None) -> dict[str, str]:
    mapping = dict(DEFAULT_FIELD_MAPPING)
    custom = (config or {}).get("field_mapping") or {}
    if isinstance(custom, dict):
        mapping.update({str(key): str(value) for key, value in custom.items() if value})
    return mapping


def field_name(config: dict[str, Any] | None, logical_name: str) -> str:
    return field_mapping(config)[logical_name]


def source_schema_expectations(config: dict[str, Any], category_type: str) -> dict[str, str]:
    fields = field_mapping(config)
    return {
        fields["task"]: "title",
        fields["category"]: category_type,
        fields["done"]: "checkbox",
        fields["takeaway"]: "rich_text",
        fields["improvement"]: "rich_text",
    }


def archive_schema_expectations(config: dict[str, Any]) -> dict[str, str]:
    fields = field_mapping(config)
    return {
        fields["task"]: "title",
        fields["takeaway"]: "rich_text",
        fields["improvement"]: "rich_text",
    }


def create_archive_row_with_config(
    client: Any,
    database_id: str,
    candidate: "Candidate",
    config: dict[str, Any],
) -> dict[str, Any]:
    if not config.get("field_mapping"):
        return client.create_archive_row(database_id, candidate)
    try:
        return client.create_archive_row(database_id, candidate, config=config)
    except TypeError as exc:
        if "config" not in str(exc):
            raise
        return client.create_archive_row(database_id, candidate)


@dataclass
class Candidate:
    source_page_id: str
    source_url: str
    name: str
    category: str
    learnings: str
    improvements: str
    created_time: str
    last_edited_time: str = ""
    category_options: list[str] | None = None

    @classmethod
    def from_notion_page(cls, page: dict[str, Any], config: dict[str, Any] | None = None) -> "Candidate":
        props = page.get("properties", {})
        fields = field_mapping(config)
        return cls(
            source_page_id=page["id"],
            source_url=page.get("url", ""),
            name=read_title(props.get(fields["task"], {})),
            category=read_text(props.get(fields["category"], {})).strip(),
            learnings=read_text(props.get(fields["takeaway"], {})).strip(),
            improvements=read_text(props.get(fields["improvement"], {})).strip(),
            created_time=page.get("created_time", ""),
            last_edited_time=page.get("last_edited_time", ""),
        )

    def to_state(self) -> dict[str, Any]:
        return {
            "source_page_id": self.source_page_id,
            "source_url": self.source_url,
            "name": self.name,
            "category": self.category,
            "learnings": self.learnings,
            "improvements": self.improvements,
            "created_time": self.created_time,
            "last_edited_time": self.last_edited_time,
            "fingerprint": candidate_fingerprint(self),
            "category_options": self.category_options or [],
        }

    @classmethod
    def from_state(cls, data: dict[str, Any]) -> "Candidate":
        return cls(
            source_page_id=data["source_page_id"],
            source_url=data.get("source_url", ""),
            name=data.get("name", ""),
            category=data.get("category", ""),
            learnings=data.get("learnings", ""),
            improvements=data.get("improvements", ""),
            created_time=data.get("created_time", ""),
            last_edited_time=data.get("last_edited_time", ""),
            category_options=data.get("category_options") or None,
        )


@dataclass
class LedgerMove:
    page_id: str
    page_url: str
    action: str
    operation_id: str
    session_id: str
    source_url: str
    target_url: str
    category: str
    batch_id: int
    batch_number: int


@dataclass
class PendingSource:
    source_page_id: str
    source_url: str
    target_url: str
    category: str
    name: str
    operation_id: str
    completed: bool


@dataclass
class ArchiveMatch:
    target_page_id: str
    target_url: str


@dataclass
class RealArchiveLookupContext:
    target_index: dict[str, Any] | None = None
    pages_by_database: dict[str, list[dict[str, Any]]] | None = None

    def __post_init__(self) -> None:
        if self.pages_by_database is None:
            self.pages_by_database = {}


@dataclass
class ArchiveTableTarget:
    category: str
    toggle_name: str
    database_name: str
    database_id: str
    database_url: str
    data_source_id: str
    data_source_url: str


class NotionClient:
    def __init__(self, token: str, source_order: str = "bottom_first", cache_path: Path = Path(DEFAULT_CACHE)) -> None:
        self.token = token
        self.source_order = source_order
        self.cache_path = cache_path

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        ensure_notion_cooldown_clear(self.cache_path, mode="api")
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.notion.com/v1{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                record_rate_limit(self.cache_path, detail, path, mode="api")
            raise WorkflowError(f"Notion API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WorkflowError(f"Network error calling Notion: {exc}") from exc

    def query_database(self, database_id: str, start_cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        if self.source_order != "bottom_first":
            raise WorkflowError(f"Unsupported source_order: {self.source_order}")
        body: dict[str, Any] = {
            "page_size": page_size,
            # Bottom-first for this workflow means older checked records first.
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
        }
        if start_cursor:
            body["start_cursor"] = start_cursor
        return self.request("POST", f"/databases/{database_id}/query", body)

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        return self.request("GET", f"/databases/{database_id}")

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self.request("GET", f"/pages/{page_id}")

    def create_archive_row(self, database_id: str, candidate: Candidate, config: dict[str, Any] | None = None) -> dict[str, Any]:
        fields = field_mapping(config)
        properties: dict[str, Any] = {
            fields["task"]: {"title": [{"text": {"content": candidate.name}}]},
            fields["takeaway"]: {"rich_text": [{"text": {"content": candidate.learnings}}]} if candidate.learnings else {"rich_text": []},
            fields["improvement"]: {"rich_text": [{"text": {"content": candidate.improvements}}]} if candidate.improvements else {"rich_text": []},
        }
        return self.request(
            "POST",
            "/pages",
            {
                "parent": {"database_id": database_id},
                "properties": properties,
            },
        )

    def retrieve_archive_tables_content(self, page_id: str) -> str:
        raise WorkflowError(
            "Real Archive 表格 writes require the Codex Notion connector, because the target is a normal page table, "
            "not a database row."
        )

    def update_archive_tables_content(self, page_id: str, content: str) -> dict[str, Any]:
        raise WorkflowError(
            "Real Archive 表格 writes require the Codex Notion connector, because the target is a normal page table, "
            "not a database row."
        )

    def create_safety_log_page(
        self,
        parent_page_id: str,
        *,
        session_id: str,
        operation_id: str,
        number: int,
        candidate: Candidate,
        selected_categories: list[str],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        lines = [
            f"status: moved-pending-source",
            f"session_id: {session_id}",
            f"operation_id: {operation_id}",
            f"batch_number: {number}",
            f"source: {candidate.source_url or candidate.source_page_id}",
            f"target: {target.get('url') or target.get('id', '')}",
            f"categories: {', '.join(selected_categories)}",
        ]
        return self.request(
            "POST",
            "/pages",
            {
                "parent": {"page_id": notion_id(parent_page_id)},
                "properties": {
                    "title": {
                        "title": [
                            {"text": {"content": f"organize safety log - {operation_id}"}},
                        ]
                    }
                },
                "children": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": "\n".join(lines)}}]},
                    }
                ],
            },
        )

    def create_ledger_row(
        self,
        database_id: str,
        *,
        session_id: str,
        operation_id: str,
        batch_id: int,
        number: int,
        candidate: Candidate,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/pages",
            {
                "parent": {"database_id": database_id},
                "properties": {
                    "Name": {"title": [{"text": {"content": candidate.name}}]},
                    "action": {"select": {"name": "moved"}},
                    "stability": {"select": {"name": "pending-removal"}},
                    "session_id": {"rich_text": [{"text": {"content": session_id}}]},
                    "operation_id": {"rich_text": [{"text": {"content": operation_id}}]},
                    "batch": {"rich_text": [{"text": {"content": str(batch_id)}}]},
                    "number": {"number": number},
                    "category": {"rich_text": [{"text": {"content": candidate.category}}]},
                    "source": {"url": candidate.source_url or None},
                    "target": {"url": target.get("url") or None},
                    "note": {"rich_text": [{"text": {"content": "created by notion_todo_workflow.py"}}]},
                },
            },
        )

    def create_manual_match_ledger_row(
        self,
        database_id: str,
        *,
        session_id: str,
        operation_id: str,
        batch_id: int,
        number: int,
        candidate: Candidate,
        target: ArchiveMatch,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/pages",
            {
                "parent": {"database_id": database_id},
                "properties": {
                    "Name": {"title": [{"text": {"content": candidate.name}}]},
                    "action": {"select": {"name": "manual-match"}},
                    "stability": {"select": {"name": "pending-removal"}},
                    "session_id": {"rich_text": [{"text": {"content": session_id}}]},
                    "operation_id": {"rich_text": [{"text": {"content": operation_id}}]},
                    "batch": {"rich_text": [{"text": {"content": str(batch_id)}}]},
                    "number": {"number": number},
                    "category": {"rich_text": [{"text": {"content": candidate.category}}]},
                    "source": {"url": candidate.source_url or None},
                    "target": {"url": target.target_url or None},
                    "note": {"rich_text": [{"text": {"content": "matched user-created archive row by notion_todo_workflow.py"}}]},
                },
            },
        )

    def create_undo_ledger_row(
        self,
        database_id: str,
        *,
        record: dict[str, Any],
        batch_id: int,
        number: int,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/pages",
            {
                "parent": {"database_id": database_id},
                "properties": {
                    "Name": {"title": [{"text": {"content": record.get("name", "undone")}}]},
                    "action": {"select": {"name": "undone"}},
                    "stability": {"select": {"name": "undone"}},
                    "session_id": {"rich_text": [{"text": {"content": record.get("session_id", "")}}]},
                    "operation_id": {"rich_text": [{"text": {"content": record.get("operation_id", "")}}]},
                    "batch": {"rich_text": [{"text": {"content": str(batch_id)}}]},
                    "number": {"number": number},
                    "category": {"rich_text": [{"text": {"content": record.get("category", "")}}]},
                    "source": {"url": record.get("source_url") or None},
                    "target": {"url": record.get("target_url") or None},
                    "note": {"rich_text": [{"text": {"content": "undo recorded by notion_todo_workflow.py"}}]},
                },
            },
        )

    def update_ledger_stability(self, page_id: str, stability: str) -> dict[str, Any]:
        return self.request("PATCH", f"/pages/{page_id}", {"properties": {"stability": {"select": {"name": stability}}}})

    def archive_page(self, page_id: str) -> dict[str, Any]:
        return self.request("PATCH", f"/pages/{page_id}", {"archived": True})


def read_title(prop: dict[str, Any]) -> str:
    return "".join(piece.get("plain_text", "") for piece in prop.get("title", []))


def read_text(prop: dict[str, Any]) -> str:
    prop_type = prop.get("type")
    if prop_type == "rich_text":
        return "".join(piece.get("plain_text", "") for piece in prop.get("rich_text", []))
    if prop_type == "select":
        selected = prop.get("select")
        return "" if selected is None else selected.get("name", "")
    if prop_type == "multi_select":
        return ", ".join(item.get("name", "") for item in prop.get("multi_select", []))
    if prop_type == "url":
        return prop.get("url") or ""
    return ""


def read_relation_ids(prop: dict[str, Any]) -> list[str]:
    if prop.get("type") != "relation":
        return []
    return [notion_id(item.get("id", "")) for item in prop.get("relation", []) if item.get("id")]


def read_checkbox(prop: dict[str, Any]) -> bool:
    return bool(prop.get("checkbox")) if prop.get("type") == "checkbox" else False


def candidate_fingerprint(candidate: Candidate) -> str:
    payload = {
        "name": normalize_fingerprint_text(candidate.name),
        "category": normalize_fingerprint_text(candidate.category),
        "learnings": normalize_fingerprint_text(candidate.learnings),
        "improvements": normalize_fingerprint_text(candidate.improvements),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_fingerprint_text(value: str) -> str:
    return (
        str(value)
        .replace("⇒", "=>")
        .replace("→", "->")
        .replace("←", "<-")
        .replace("’", "'")
        .strip()
    )


def archive_content(page: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, str]:
    props = page.get("properties", {})
    fields = field_mapping(config)
    return {
        "name": read_title(props.get(fields["task"], {})).strip(),
        "learnings": read_text(props.get(fields["takeaway"], {})).strip(),
        "improvements": read_text(props.get(fields["improvement"], {})).strip(),
    }


def candidate_content(candidate: Candidate) -> dict[str, str]:
    return {
        "name": candidate.name.strip(),
        "learnings": candidate.learnings.strip(),
        "improvements": candidate.improvements.strip(),
    }


def page_content_matches_candidate(page: dict[str, Any], candidate: Candidate, config: dict[str, Any] | None = None) -> bool:
    return archive_content(page, config) == candidate_content(candidate)


def operation_id(session_id: str, source_page_id: str, batch_number: int) -> str:
    seed = f"{session_id}:{source_page_id}:{batch_number}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def operation_attempt_id(base_operation_id: str, attempt: int) -> str:
    return base_operation_id if attempt == 0 else f"{base_operation_id}-r{attempt}"


def next_operation_id(state: dict[str, Any], session_id: str, source_page_id: str, batch_number: int) -> str:
    base = operation_id(session_id, source_page_id, batch_number)
    blocked = {
        item.get("operation_id")
        for item in state.get("undone", []) + state.get("moved", []) + state.get("manual_matches", []) + state.get("dismissed", [])
        if item.get("operation_id")
    }
    attempt = 0
    while operation_attempt_id(base, attempt) in blocked:
        attempt += 1
    return operation_attempt_id(base, attempt)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_seconds() -> float:
    return time.time()


def load_cache(path: Path = Path(DEFAULT_CACHE)) -> dict[str, Any]:
    return load_json(path, {})


def save_cache(cache: dict[str, Any], path: Path = Path(DEFAULT_CACHE)) -> None:
    save_json(path, cache)


def config_signature(config: dict[str, Any]) -> str:
    relevant = {
        "source_database_id": config.get("source_database_id"),
        "ledger_database_id": config.get("ledger_database_id"),
        "target_databases": config.get("target_databases", {}),
        "archive_tables_page_id": config.get("archive_tables_page_id"),
        "archive_tables": config.get("archive_tables", {}),
        "project_categories": config.get("project_categories", {}),
        "source_order": config.get("source_order"),
        "batch_size": config.get("batch_size"),
        "move_limit": config.get("move_limit"),
        "mode": config.get("mode", TEST_MODE),
    }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cooldown_mode(cooldown: dict[str, Any]) -> str:
    mode = cooldown.get("mode")
    if mode:
        return str(mode)
    step = str(cooldown.get("step", ""))
    return "connector" if step.startswith("connector ") else "api"


def ensure_notion_cooldown_clear(cache_path: Path = Path(DEFAULT_CACHE), *, mode: str = "api") -> None:
    cooldown = load_cache(cache_path).get("notion_rate_limit", {})
    if cooldown and cooldown_mode(cooldown) != mode:
        return
    until = float(cooldown.get("until", 0) or 0)
    remaining = int(round(until - now_seconds()))
    if remaining > 0:
        step = cooldown.get("step", "Notion request")
        raise WorkflowError(f"Notion cooldown active for ~{remaining}s after rate limit at {step}. Try again later.")


def record_rate_limit(cache_path: Path, detail: str, step: str, *, mode: str = "api") -> None:
    retry_after = 30
    try:
        payload = json.loads(detail)
        retry_after = int(payload.get("additional_data", {}).get("retry_after") or retry_after)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    cache = load_cache(cache_path)
    cache["notion_rate_limit"] = {
        "mode": mode,
        "until": now_seconds() + max(1, retry_after),
        "retry_after": retry_after,
        "step": step,
        "recorded_at": now_seconds(),
    }
    save_cache(cache, cache_path)


def preflight_cache_fresh(config: dict[str, Any], cache_path: Path = Path(DEFAULT_CACHE)) -> bool:
    preflight_state = load_cache(cache_path).get("preflight", {})
    if preflight_state.get("config_signature") != config_signature(config):
        return False
    checked_at = float(preflight_state.get("checked_at", 0) or 0)
    return now_seconds() - checked_at <= PREFLIGHT_TTL_SECONDS


def mark_preflight_ok(config: dict[str, Any], cache_path: Path = Path(DEFAULT_CACHE)) -> None:
    cache = load_cache(cache_path)
    cache["preflight"] = {"config_signature": config_signature(config), "checked_at": now_seconds()}
    save_cache(cache, cache_path)


def cached_ledger_effective_moves(config: dict[str, Any], cache_path: Path = Path(DEFAULT_CACHE)) -> tuple[set[str], set[str]] | None:
    ledger = load_cache(cache_path).get("ledger_effective_moves", {})
    if ledger.get("config_signature") != config_signature(config):
        return None
    checked_at = float(ledger.get("checked_at", 0) or 0)
    if now_seconds() - checked_at > LEDGER_CACHE_TTL_SECONDS:
        return None
    return set(ledger.get("source_ids", [])), set(ledger.get("operation_ids", []))


def mark_ledger_effective_moves(
    config: dict[str, Any],
    source_ids: set[str],
    operation_ids: set[str],
    cache_path: Path = Path(DEFAULT_CACHE),
) -> None:
    cache = load_cache(cache_path)
    cache["ledger_effective_moves"] = {
        "config_signature": config_signature(config),
        "checked_at": now_seconds(),
        "source_ids": sorted(source_ids),
        "operation_ids": sorted(operation_ids),
    }
    save_cache(cache, cache_path)


def archive_target_payload(target: ArchiveTableTarget) -> dict[str, str]:
    return {
        "category": target.category,
        "toggle_name": target.toggle_name,
        "database_name": target.database_name,
        "database_id": target.database_id,
        "database_url": target.database_url,
        "data_source_id": target.data_source_id,
        "data_source_url": target.data_source_url,
    }


def archive_target_from_payload(payload: dict[str, Any]) -> ArchiveTableTarget:
    return ArchiveTableTarget(
        category=str(payload.get("category", "")),
        toggle_name=str(payload.get("toggle_name", "")),
        database_name=str(payload.get("database_name", "")),
        database_id=notion_id(str(payload.get("database_id", ""))),
        database_url=str(payload.get("database_url", "")),
        data_source_id=str(payload.get("data_source_id", "")),
        data_source_url=str(payload.get("data_source_url", "")),
    )


def archive_target_index_from_content(content: str) -> dict[str, Any]:
    targets = [archive_target_payload(target) for target in archive_table_targets(content)]
    return {
        "targets": targets,
        "names": sorted(
            {
                name
                for target in targets
                for name in (target.get("toggle_name", ""), target.get("database_name", ""))
                if name
            }
        ),
    }


def cached_archive_target_index(config: dict[str, Any], cache_path: Path = Path(DEFAULT_CACHE)) -> dict[str, Any] | None:
    target_cache = load_cache(cache_path).get("real_archive_targets", {})
    if target_cache.get("config_signature") != config_signature(config):
        return None
    if notion_id(str(target_cache.get("archive_tables_page_id", ""))) != notion_id(str(config.get("archive_tables_page_id", ""))):
        return None
    checked_at = float(target_cache.get("checked_at", 0) or 0)
    if now_seconds() - checked_at > ARCHIVE_TARGET_CACHE_TTL_SECONDS:
        return None
    return {
        "targets": list(target_cache.get("targets", [])),
        "names": list(target_cache.get("names", [])),
    }


def mark_archive_target_index(
    config: dict[str, Any],
    index: dict[str, Any],
    cache_path: Path = Path(DEFAULT_CACHE),
) -> None:
    cache = load_cache(cache_path)
    cache["real_archive_targets"] = {
        "config_signature": config_signature(config),
        "archive_tables_page_id": notion_id(str(config.get("archive_tables_page_id", ""))),
        "checked_at": now_seconds(),
        "targets": list(index.get("targets", [])),
        "names": list(index.get("names", [])),
    }
    save_cache(cache, cache_path)


def backup_helper_path() -> Path | None:
    if BACKUP_HELPER.exists():
        return BACKUP_HELPER
    installed = Path.home() / ".codex/skills/todo-archive-review/scripts/todo_archive_backup.py"
    return installed if installed.exists() else None


def run_backup(args: list[str], input_json: dict[str, Any] | None = None) -> str:
    helper = backup_helper_path()
    if helper is None:
        raise WorkflowError("Backup helper not found.")
    proc = subprocess.run(
        [sys.executable, str(helper), *args],
        input=None if input_json is None else json.dumps(input_json, ensure_ascii=False),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise WorkflowError(f"Backup helper failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def ensure_backup_session(state: dict[str, Any], label: str = "todo-test") -> str:
    session_id = state.get("backup_session_id")
    if session_id:
        return session_id
    session_id = run_backup(["begin", "--label", label])
    state["backup_session_id"] = session_id
    state["session_id"] = session_id
    return session_id


def begin_new_backup_session(state: dict[str, Any], label: str = "todo-test") -> str:
    session_id = run_backup(["begin", "--label", label])
    state["backup_session_id"] = session_id
    state["session_id"] = session_id
    return session_id


def finish_backup_session(state: dict[str, Any], status: str) -> None:
    session_id = state.get("backup_session_id") or state.get("session_id")
    if not session_id:
        return
    run_backup(["finish", session_id, "--status", status])


def backup_record(state: dict[str, Any], record: dict[str, Any]) -> None:
    session_id = ensure_backup_session(state)
    run_backup(["add", session_id], input_json=record)


def backup_active_batch(state: dict[str, Any]) -> None:
    session_id = ensure_backup_session(state)
    batch_id = int(state.get("batch_id", 0))
    for number, item in enumerate(state.get("batch", []), start=1):
        candidate = Candidate.from_state(item)
        run_backup(
            ["add", session_id],
            input_json={
                "action": "candidate",
                "status": "candidate",
                "batch": batch_id,
                "number": number,
                "category": candidate.category,
                "name_hint": candidate.name,
                "source_page_id": candidate.source_page_id,
                "source_url": candidate.source_url,
            },
        )


def load_config(path: Path) -> dict[str, Any]:
    config = load_json(path, {})
    mode = config.get("mode", TEST_MODE)
    if mode not in {TEST_MODE, REAL_MODE}:
        raise WorkflowError("config mode must be `test` or `real`")
    if mode == REAL_MODE:
        required = ["source_database_id", "archive_tables_page_id", "archive_tables", "project_categories"]
    else:
        required = ["source_database_id", "target_databases"]
    missing = [key for key in required if key not in config]
    if missing:
        raise WorkflowError(f"Missing config keys: {', '.join(missing)}")
    if mode == REAL_MODE:
        forbidden = [key for key in ("safety_log_parent_page_id", "ledger_database_id") if config.get(key)]
        if forbidden:
            raise WorkflowError(
                "Real mode must not write Notion safety/log/ledger pages. "
                "Remove these config keys and use minimal local .todo_archive state instead: "
                + ", ".join(forbidden)
            )
    config["mode"] = mode
    return config


def config_mode(config: dict[str, Any]) -> str:
    return str(config.get("mode", TEST_MODE))


def require_writable_mode(config: dict[str, Any], command: str) -> None:
    if config_mode(config) == REAL_MODE:
        raise WorkflowError(
            f"`{command}` is not enabled for real mode."
        )


def require_real_command_allowed(config: dict[str, Any], command: str) -> None:
    if config_mode(config) == TEST_MODE:
        return
    allowed = {"next", "ok", "dismiss", "skip", "undo", "save", "done", "confirm", "remove-sources", "check", "status", "discard"}
    if command not in allowed:
        raise WorkflowError(
            f"`{command}` is not enabled for real mode."
        )


def positive_int(value: Any, *, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise WorkflowError(f"{name} must be a positive integer")
    return parsed


def organize_options(config: dict[str, Any], args: argparse.Namespace | None = None) -> dict[str, int]:
    batch_size = getattr(args, "batch_size", None) if args is not None else None
    move_limit = getattr(args, "move_limit", None) if args is not None else None
    return {
        "batch_size": positive_int(batch_size if batch_size is not None else config.get("batch_size", 5), name="batch_size"),
        "move_limit": positive_int(move_limit if move_limit is not None else config.get("move_limit", 30), name="move_limit"),
    }


def session_options(state: dict[str, Any], config: dict[str, Any], args: argparse.Namespace | None = None) -> dict[str, int]:
    existing = state.get("session_options")
    if isinstance(existing, dict):
        return {
            "batch_size": positive_int(existing.get("batch_size", config.get("batch_size", 5)), name="batch_size"),
            "move_limit": positive_int(existing.get("move_limit", config.get("move_limit", 30)), name="move_limit"),
        }
    options = organize_options(config, args)
    state["session_options"] = options
    return options


def print_options(options: dict[str, int]) -> None:
    print(f"Batch size: {options['batch_size']} | Session limit: {options['move_limit']}")


def require_props(db: dict[str, Any], expected: dict[str, str], label: str) -> None:
    props = db.get("properties", {})
    problems = []
    for name, prop_type in expected.items():
        actual = props.get(name, {}).get("type")
        if actual != prop_type:
            problems.append(f"{name} expected {prop_type}, got {actual or 'missing'}")
    if problems:
        raise WorkflowError(f"{label} schema mismatch: " + "; ".join(problems))


def preflight(config: dict[str, Any], *, force: bool = False, cache_path: Path = Path(DEFAULT_CACHE)) -> None:
    if not force and preflight_cache_fresh(config, cache_path):
        return
    ensure_notion_cooldown_clear(cache_path)
    client = get_client(config.get("source_order", "bottom_first"))
    if config_mode(config) == REAL_MODE:
        require_props(
            client.retrieve_database(notion_id(config["source_database_id"])),
            source_schema_expectations(config, "relation"),
            "source",
        )
        archive_tables = config.get("archive_tables") or {}
        missing_tables = [category for category in config.get("project_categories", {}) if category not in archive_tables]
        if missing_tables:
            raise WorkflowError("Missing Archive 表格 table mappings: " + ", ".join(sorted(missing_tables)))
    else:
        require_props(
            client.retrieve_database(notion_id(config["source_database_id"])),
            source_schema_expectations(config, "rich_text"),
            "source",
        )
        for category, database_id in config["target_databases"].items():
            require_props(
                client.retrieve_database(notion_id(database_id)),
                archive_schema_expectations(config),
                f"archive {category}",
            )
    ledger_id = None if config_mode(config) == REAL_MODE else config.get("ledger_database_id")
    if ledger_id:
        require_props(
            client.retrieve_database(notion_id(ledger_id)),
            {
                "Name": "title",
                "action": "select",
                "stability": "select",
                "session_id": "rich_text",
                "operation_id": "rich_text",
                "source": "url",
                "target": "url",
                "category": "rich_text",
                "batch": "rich_text",
                "number": "number",
                "note": "rich_text",
            },
            "ledger",
        )
    mark_preflight_ok(config, cache_path)


def notion_id(raw: str) -> str:
    return raw.replace("-", "")


def page_id_from_url(url: str) -> str:
    compact = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1].replace("-", "")
    if len(compact) >= 32:
        return compact[-32:]
    return compact


def archive_table_name(config: dict[str, Any], category: str) -> str:
    tables = config.get("archive_tables", {})
    name = tables.get(category, category)
    if not name:
        raise WorkflowError(f"No Archive 表格 target configured for category: {category}")
    return str(name)


def normalize_archive_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def archive_table_targets(content: str) -> list[ArchiveTableTarget]:
    targets: list[ArchiveTableTarget] = []
    detail_pattern = re.compile(r"<details>\s*<summary>(.*?)</summary>(.*?)</details>", flags=re.S)
    database_pattern = re.compile(
        r'<database\s+url="([^"]+)"\s+inline="true"\s+data-source-url="collection://([^"]+)">(.*?)</database>',
        flags=re.S,
    )
    for detail in detail_pattern.finditer(content):
        toggle_name = detail.group(1).strip()
        database = database_pattern.search(detail.group(2))
        if not database:
            continue
        database_url = database.group(1)
        data_source_id = database.group(2)
        database_name = re.sub(r"<[^>]+>", "", database.group(3)).strip()
        targets.append(
            ArchiveTableTarget(
                category=toggle_name,
                toggle_name=toggle_name,
                database_name=database_name,
                database_id=page_id_from_url(database_url),
                database_url=database_url,
                data_source_id=data_source_id,
                data_source_url=f"collection://{data_source_id}",
            )
        )
    return targets


def find_archive_table_target(content: str, expected_name: str) -> ArchiveTableTarget | None:
    expected = normalize_archive_name(expected_name)
    for target in archive_table_targets(content):
        if normalize_archive_name(target.toggle_name) == expected and normalize_archive_name(target.database_name) == expected:
            return target
    for target in archive_table_targets(content):
        if normalize_archive_name(target.toggle_name) == expected or normalize_archive_name(target.database_name) == expected:
            return target
    return None


def similar_archive_table_names(content: str, expected_name: str, *, limit: int = 3) -> list[str]:
    names = sorted({target.toggle_name for target in archive_table_targets(content)} | {target.database_name for target in archive_table_targets(content)})
    lookup = {normalize_archive_name(name): name for name in names}
    matches = get_close_matches(normalize_archive_name(expected_name), list(lookup.keys()), n=limit, cutoff=0.55)
    return [lookup[match] for match in matches]


def require_archive_table_target(config: dict[str, Any], content: str, category: str) -> ArchiveTableTarget:
    expected_name = archive_table_name(config, category)
    target = find_archive_table_target(content, expected_name)
    if target is not None:
        return target
    suggestions = similar_archive_table_names(content, expected_name)
    if suggestions:
        raise WorkflowError(
            f"Archive 表格 has no exact toggle/database pair for `{expected_name}`. "
            f"Ask in the batch summary whether to use a similar table: {', '.join(suggestions)}."
        )
    raise WorkflowError(
        f"Archive 表格 has no toggle/database pair for `{expected_name}`. "
        "Ask in the batch summary whether to create a new toggle-table pair with matching names."
    )


def find_archive_table_target_in_index(index: dict[str, Any], expected_name: str) -> ArchiveTableTarget | None:
    expected = normalize_archive_name(expected_name)
    targets = [archive_target_from_payload(item) for item in index.get("targets", [])]
    for target in targets:
        if normalize_archive_name(target.toggle_name) == expected and normalize_archive_name(target.database_name) == expected:
            return target
    for target in targets:
        if normalize_archive_name(target.toggle_name) == expected or normalize_archive_name(target.database_name) == expected:
            return target
    return None


def similar_archive_table_names_in_index(index: dict[str, Any], expected_name: str, *, limit: int = 3) -> list[str]:
    names = sorted({str(name) for name in index.get("names", []) if name})
    lookup = {normalize_archive_name(name): name for name in names}
    matches = get_close_matches(normalize_archive_name(expected_name), list(lookup.keys()), n=limit, cutoff=0.55)
    return [lookup[match] for match in matches]


def require_archive_table_target_from_index(config: dict[str, Any], index: dict[str, Any], category: str) -> ArchiveTableTarget:
    expected_name = archive_table_name(config, category)
    target = find_archive_table_target_in_index(index, expected_name)
    if target is not None:
        return target
    suggestions = similar_archive_table_names_in_index(index, expected_name)
    if suggestions:
        raise WorkflowError(
            f"Archive 表格 has no exact toggle/database pair for `{expected_name}`. "
            f"Ask in the batch summary whether to use a similar table: {', '.join(suggestions)}."
        )
    raise WorkflowError(
        f"Archive 表格 has no toggle/database pair for `{expected_name}`. "
        "Ask in the batch summary whether to create a new toggle-table pair with matching names."
    )


def real_archive_target_index(
    config: dict[str, Any],
    client: NotionClient,
    context: RealArchiveLookupContext | None = None,
) -> dict[str, Any]:
    if context is not None and context.target_index is not None:
        return context.target_index
    cache_path = getattr(client, "cache_path", Path(DEFAULT_CACHE))
    cached = cached_archive_target_index(config, cache_path)
    if cached is not None:
        if context is not None:
            context.target_index = cached
        return cached
    page_id = notion_id(str(config["archive_tables_page_id"]))
    content = client.retrieve_archive_tables_content(page_id)
    index = archive_target_index_from_content(content)
    mark_archive_target_index(config, index, cache_path)
    if context is not None:
        context.target_index = index
    return index


def real_archive_table_target(
    config: dict[str, Any],
    client: NotionClient,
    category: str,
    context: RealArchiveLookupContext | None = None,
) -> ArchiveTableTarget:
    return require_archive_table_target_from_index(config, real_archive_target_index(config, client, context), category)


def real_archive_table_target_url(config: dict[str, Any], category: str) -> str:
    name = archive_table_name(config, category)
    return f"https://app.notion.com/{notion_id(str(config['archive_tables_page_id']))}#{name}"


def real_archive_database_pages(
    client: NotionClient,
    database_id: str,
    context: RealArchiveLookupContext | None = None,
) -> list[dict[str, Any]]:
    compact = notion_id(database_id)
    if context is not None and context.pages_by_database is not None and compact in context.pages_by_database:
        return context.pages_by_database[compact]
    pages = iter_source_pages(client, compact)
    if context is not None and context.pages_by_database is not None:
        context.pages_by_database[compact] = pages
    return pages


def synthetic_archive_page_from_candidate(
    page_id: str,
    url: str,
    database_id: str,
    candidate: Candidate,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields = field_mapping(config)
    return {
        "id": page_id,
        "url": url,
        "archived": False,
        "parent": {"type": "database_id", "database_id": notion_id(database_id)},
        "properties": {
            fields["task"]: {"type": "title", "title": [{"plain_text": candidate.name}]},
            fields["takeaway"]: {"type": "rich_text", "rich_text": [{"plain_text": candidate.learnings}]},
            fields["improvement"]: {"type": "rich_text", "rich_text": [{"plain_text": candidate.improvements}]},
        },
    }


def create_real_archive_table_row(
    config: dict[str, Any],
    client: NotionClient,
    candidate: Candidate,
    context: RealArchiveLookupContext | None = None,
) -> dict[str, Any]:
    target = real_archive_table_target(config, client, candidate.category, context)
    for page in real_archive_database_pages(client, target.database_id, context):
        if not page.get("archived") and page_content_matches_candidate(page, candidate, config):
            return {"id": page.get("id", ""), "url": page.get("url", target.database_url), "manual_match": True}
    created = create_archive_row_with_config(client, notion_id(target.database_id), candidate, config)
    created["manual_match"] = False
    if context is not None and context.pages_by_database is not None:
        compact = notion_id(target.database_id)
        if compact in context.pages_by_database:
            context.pages_by_database[compact].append(
                synthetic_archive_page_from_candidate(
                    str(created.get("id", "")),
                    str(created.get("url", "")),
                    target.database_id,
                    candidate,
                    config,
                )
            )
    return created


def page_database_id(page: dict[str, Any]) -> str:
    parent = page.get("parent", {})
    if parent.get("type") == "database_id":
        return notion_id(parent.get("database_id", ""))
    return ""


def read_keychain_token() -> str:
    if sys.platform != "darwin":
        return ""
    proc = subprocess.run(
        ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", "codex-notion-token", "-w"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def get_client(source_order: str = "bottom_first") -> NotionClient:
    token = os.environ.get("NOTION_TOKEN") or read_keychain_token()
    if not token:
        raise WorkflowError(
            "This external CLI fallback requires NOTION_TOKEN or a macOS Keychain item named codex-notion-token. "
            "Inside Codex, use the todo-archive-review skill with the Notion connector when fallback is not explicitly needed."
        )
    return NotionClient(token, source_order=source_order)


def iter_source_pages(client: NotionClient, database_id: str) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor = None
    while True:
        result = client.query_database(database_id, start_cursor=cursor)
        pages.extend(result.get("results", []))
        if not result.get("has_more"):
            return pages
        cursor = result.get("next_cursor")


def iter_limited_source_page_batches(
    client: NotionClient,
    database_id: str,
    page_size: int,
    *,
    max_pages: int = 10,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    cursor = None
    for _ in range(max_pages):
        result = client.query_database(database_id, start_cursor=cursor, page_size=page_size)
        batches.append(result.get("results", []))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return batches


def eligible_candidate(page: dict[str, Any], config: dict[str, Any] | None = None) -> Candidate | None:
    props = page.get("properties", {})
    if not read_checkbox(props.get(field_name(config, "done"), {})):
        return None
    candidate = Candidate.from_notion_page(page, config)
    if not candidate.category:
        return None
    if not candidate.learnings and not candidate.improvements:
        return None
    return candidate


def real_category_lookup(config: dict[str, Any]) -> dict[str, str]:
    return {
        notion_id(page_id): name
        for name, page_id in config.get("project_categories", {}).items()
        if page_id
    }


def real_candidate_from_notion_page(page: dict[str, Any], config: dict[str, Any]) -> Candidate:
    props = page.get("properties", {})
    fields = field_mapping(config)
    lookup = real_category_lookup(config)
    relation_ids = read_relation_ids(props.get(fields["category"], {}))
    category_options = [lookup[relation_id] for relation_id in relation_ids if relation_id in lookup]
    unknown = [relation_id for relation_id in relation_ids if relation_id not in lookup]
    category_labels = category_options + [f"unknown:{item[:8]}" for item in unknown]
    return Candidate(
        source_page_id=page["id"],
        source_url=page.get("url", ""),
        name=read_title(props.get(fields["task"], {})),
        category=" + ".join(category_labels),
        learnings=read_text(props.get(fields["takeaway"], {})).strip(),
        improvements=read_text(props.get(fields["improvement"], {})).strip(),
        created_time=page.get("created_time", ""),
        last_edited_time=page.get("last_edited_time", ""),
        category_options=category_options,
    )


def real_eligible_candidate(page: dict[str, Any], config: dict[str, Any]) -> Candidate | None:
    props = page.get("properties", {})
    if not read_checkbox(props.get(field_name(config, "done"), {})):
        return None
    candidate = real_candidate_from_notion_page(page, config)
    if not candidate.category_options:
        return None
    if not candidate.learnings and not candidate.improvements:
        return None
    return candidate


def real_empty_checked_candidate(page: dict[str, Any], config: dict[str, Any]) -> Candidate | None:
    props = page.get("properties", {})
    if not read_checkbox(props.get(field_name(config, "done"), {})):
        return None
    candidate = real_candidate_from_notion_page(page, config)
    if candidate.learnings or candidate.improvements:
        return None
    return candidate


def empty_checked_candidate(page: dict[str, Any], config: dict[str, Any] | None = None) -> Candidate | None:
    props = page.get("properties", {})
    if not read_checkbox(props.get(field_name(config, "done"), {})):
        return None
    candidate = Candidate.from_notion_page(page, config)
    if candidate.learnings or candidate.improvements:
        return None
    return candidate


def remove_empty_checked_row(
    state: dict[str, Any],
    client: NotionClient,
    page: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = empty_checked_candidate(page, config)
    if candidate is None:
        raise WorkflowError("Internal error: source row is not an empty checked row.")
    batch_id = int(state.get("batch_id", 0))
    record = {
        "action": "empty-removed",
        "status": "removed",
        "batch": batch_id,
        "category": candidate.category,
        "name_hint": candidate.name,
        "source_page_id": candidate.source_page_id,
        "source_url": candidate.source_url,
        "removed_at": now_seconds(),
    }
    backup_record(state, record)
    client.archive_page(candidate.source_page_id)
    state.setdefault("empty_removed", []).append(record)
    state.setdefault("_empty_removed_this_batch", []).append(record)
    return record


def undone_operation_ids(state: dict[str, Any]) -> set[str]:
    return {
        item["operation_id"]
        for item in state.get("undone", [])
        if item.get("operation_id")
    }


def undone_source_ids_without_operation(state: dict[str, Any]) -> set[str]:
    return {
        notion_id(item["source_page_id"])
        for item in state.get("undone", [])
        if item.get("source_page_id") and not item.get("operation_id")
    }


def effective_records(records: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    undone_ops = undone_operation_ids(state)
    undone_sources_without_op = undone_source_ids_without_operation(state)
    effective: list[dict[str, Any]] = []
    for item in records:
        op_id = item.get("operation_id")
        source_id = notion_id(item.get("source_page_id", ""))
        if op_id and op_id in undone_ops:
            continue
        if not op_id and source_id and source_id in undone_sources_without_op:
            continue
        effective.append(item)
    return effective


def moved_source_ids(state: dict[str, Any]) -> set[str]:
    return {
        notion_id(item["source_page_id"])
        for item in effective_records(state.get("moved", []), state)
        if item.get("source_page_id")
    }


def manual_match_source_ids(state: dict[str, Any]) -> set[str]:
    return {
        notion_id(item["source_page_id"])
        for item in effective_records(state.get("manual_matches", []), state)
        if item.get("source_page_id")
    }


def skipped_source_ids(state: dict[str, Any]) -> set[str]:
    source_ids: set[str] = set()
    for item in state.get("skipped", []):
        if isinstance(item, dict):
            if item.get("source_page_id"):
                source_ids.add(notion_id(item["source_page_id"]))
        else:
            source_ids.add(notion_id(item))
    return source_ids


def dismissed_source_ids(state: dict[str, Any]) -> set[str]:
    return {
        notion_id(item["source_page_id"])
        for item in state.get("dismissed", [])
        if item.get("source_page_id")
    }


def dismissed_operation_ids(state: dict[str, Any]) -> set[str]:
    return {
        item["operation_id"]
        for item in state.get("dismissed", [])
        if item.get("operation_id")
    }


def ledger_effective_moves(config: dict[str, Any], client: NotionClient) -> tuple[set[str], set[str]]:
    ledger_id = None if config_mode(config) == REAL_MODE else config.get("ledger_database_id")
    if not ledger_id:
        return set(), set()
    moved_ops: dict[str, str] = {}
    moved_without_op: set[str] = set()
    undone_ops: set[str] = set()
    for page in iter_source_pages(client, notion_id(ledger_id)):
        props = page.get("properties", {})
        source = read_text(props.get("source", {}))
        op_id = read_text(props.get("operation_id", {}))
        action = read_text(props.get("action", {}))
        source_id = page_id_from_url(source) if source else ""
        if action in {"moved", "manual-match"}:
            if op_id:
                moved_ops[op_id] = source_id
            elif source_id:
                moved_without_op.add(source_id)
        elif action == "undone":
            if op_id:
                undone_ops.add(op_id)
    effective_ops = set(moved_ops) - undone_ops
    effective_sources = {moved_ops[op_id] for op_id in effective_ops if moved_ops.get(op_id)}
    effective_sources |= moved_without_op
    mark_ledger_effective_moves(config, effective_sources, effective_ops, client.cache_path)
    return effective_sources, effective_ops


def ledger_move_from_page(page: dict[str, Any]) -> LedgerMove | None:
    props = page.get("properties", {})
    action = read_text(props.get("action", {}))
    if action not in {"moved", "manual-match"}:
        return None
    op_id = read_text(props.get("operation_id", {}))
    source_url = read_text(props.get("source", {}))
    target_url = read_text(props.get("target", {}))
    if not op_id or not source_url or not target_url:
        return None
    return LedgerMove(
        page_id=page.get("id", ""),
        page_url=page.get("url", ""),
        action=action,
        operation_id=op_id,
        session_id=read_text(props.get("session_id", {})),
        source_url=source_url,
        target_url=target_url,
        category=read_text(props.get("category", {})),
        batch_id=int(read_text(props.get("batch", {})) or 0),
        batch_number=int(props.get("number", {}).get("number") or 0),
    )


def ledger_pending_moves(config: dict[str, Any], client: NotionClient) -> list[LedgerMove]:
    ledger_id = None if config_mode(config) == REAL_MODE else config.get("ledger_database_id")
    if not ledger_id:
        return []
    moved_by_op: dict[str, LedgerMove] = {}
    moved_without_op: list[LedgerMove] = []
    undone_ops: set[str] = set()
    for page in iter_source_pages(client, notion_id(ledger_id)):
        props = page.get("properties", {})
        action = read_text(props.get("action", {}))
        op_id = read_text(props.get("operation_id", {}))
        if action == "undone" and op_id:
            undone_ops.add(op_id)
            continue
        move = ledger_move_from_page(page)
        if move is None:
            continue
        stability = read_text(props.get("stability", {}))
        if stability == "removed":
            continue
        if move.operation_id:
            moved_by_op[move.operation_id] = move
        else:
            moved_without_op.append(move)
    return [move for op_id, move in moved_by_op.items() if op_id not in undone_ops] + moved_without_op


def pending_moved_sources_still_in_source(config: dict[str, Any], client: NotionClient) -> list[PendingSource]:
    source_id = notion_id(config["source_database_id"])
    source_pages = {
        notion_id(page["id"]): page
        for page in iter_source_pages(client, source_id)
        if not page.get("archived")
    }
    pending: list[PendingSource] = []
    for move in ledger_pending_moves(config, client):
        source_page_id = page_id_from_url(move.source_url)
        source_page = source_pages.get(source_page_id)
        if source_page is None:
            continue
        fields = field_mapping(config)
        pending.append(
            PendingSource(
                source_page_id=source_page_id,
                source_url=move.source_url,
                target_url=move.target_url,
                category=move.category,
                name=read_title(source_page.get("properties", {}).get(fields["task"], {})),
                operation_id=move.operation_id,
                completed=read_checkbox(source_page.get("properties", {}).get(fields["done"], {})),
            )
        )
    return pending


def print_pending_moved_sources(pending: list[PendingSource], config: dict[str, Any] | None = None) -> None:
    if not pending:
        return
    completed = [item for item in pending if item.completed]
    unchecked = [item for item in pending if not item.completed]
    if completed:
        print("Already categorized and completed, still waiting for final source removal:")
    for index, item in enumerate(completed, start=1):
        print(f"  {index}. [{item.category}] {item.name}")
        print(f"     source: {item.source_url}")
        print(f"     target: {item.target_url}")
    if completed:
        print("These are hidden from new candidate batches to prevent duplicate copies.")
    if unchecked:
        print("Needs review: categorized in ledger, but source row is not checked complete:")
        for index, item in enumerate(unchecked, start=1):
            print(f"  {index}. [{item.category}] {item.name}")
            print(f"     source: {item.source_url}")
            print(f"     target: {item.target_url}")
        print(f"These will not be removed from the source table until {field_name(config, 'done')} is checked.")


def matching_ledger_move(
    config: dict[str, Any],
    client: NotionClient,
    record: dict[str, Any],
    *,
    allowed_actions: set[str] | None = None,
) -> LedgerMove:
    ledger_id = None if config_mode(config) == REAL_MODE else config.get("ledger_database_id")
    if not ledger_id:
        raise WorkflowError("Ledger verification is required before undo or source removal.")

    record_op = record.get("operation_id", "")
    record_source_id = notion_id(record.get("source_page_id", ""))
    record_target_id = notion_id(record.get("target_page_id", ""))
    if not record_op or not record_source_id or not record_target_id:
        raise WorkflowError("Moved record is missing operation/source/target evidence.")

    undone_ops: set[str] = set()
    candidates: list[LedgerMove] = []
    for page in iter_source_pages(client, notion_id(ledger_id)):
        props = page.get("properties", {})
        action = read_text(props.get("action", {}))
        op_id = read_text(props.get("operation_id", {}))
        if action == "undone" and op_id:
            undone_ops.add(op_id)
            continue
        move = ledger_move_from_page(page)
        if move and move.operation_id == record_op:
            if allowed_actions is not None and move.action not in allowed_actions:
                continue
            candidates.append(move)

    if record_op in undone_ops:
        raise WorkflowError(f"Ledger shows operation already undone: {record_op}")

    for move in candidates:
        if page_id_from_url(move.source_url) != record_source_id:
            continue
        if page_id_from_url(move.target_url) != record_target_id:
            continue
        if record.get("session_id") and move.session_id != record.get("session_id"):
            continue
        if record.get("category") and move.category != record.get("category"):
            continue
        if record.get("batch_id") is not None and move.batch_id != int(record.get("batch_id") or 0):
            continue
        if record.get("batch_number") is not None and move.batch_number != int(record.get("batch_number") or 0):
            continue
        return move

    raise WorkflowError("Moved record does not match an effective Notion ledger row.")


def verify_page_scope(client: NotionClient, page_id: str, database_id: str, label: str) -> dict[str, Any]:
    page = client.retrieve_page(page_id)
    if page.get("archived"):
        raise WorkflowError(f"{label} page is already archived: {page_id}")
    expected = notion_id(database_id)
    actual = page_database_id(page)
    if actual != expected:
        raise WorkflowError(f"{label} page is outside the expected database: {page_id}")
    return page


def verify_moved_record_for_destructive_action(
    config: dict[str, Any],
    client: NotionClient,
    record: dict[str, Any],
    *,
    require_source_completed: bool = False,
    require_target_content_current: bool = False,
    allowed_actions: set[str] | None = None,
) -> LedgerMove:
    if config_mode(config) == REAL_MODE and not config.get("ledger_database_id"):
        move = LedgerMove(
            page_id="",
            page_url="",
            action="moved",
            operation_id=record.get("operation_id", ""),
            session_id=record.get("session_id", ""),
            source_url=record.get("source_url", ""),
            target_url=record.get("target_url", ""),
            category=record.get("category", ""),
            batch_id=int(record.get("batch_id") or 0),
            batch_number=int(record.get("batch_number") or 0),
        )
    else:
        move = matching_ledger_move(config, client, record, allowed_actions=allowed_actions)
    category = record.get("category", "")
    source_page = verify_page_scope(client, record["source_page_id"], config["source_database_id"], "Source")
    done_field = field_name(config, "done")
    if require_source_completed and not read_checkbox(source_page.get("properties", {}).get(done_field, {})):
        raise WorkflowError(
            "Final source removal stopped: source row is not checked complete. "
            f"Review `{done_field}` before removing: {record.get('source_url') or record.get('source_page_id')}"
        )
    if config_mode(config) == REAL_MODE:
        if require_target_content_current:
            source_candidate = real_candidate_from_notion_page(source_page, config)
            if category not in (source_candidate.category_options or [source_candidate.category]):
                raise WorkflowError(
                    "Final source removal stopped: source category changed while pending removal. "
                    f"Review before removing: {record.get('source_url') or record.get('source_page_id')}"
                )
            target_candidate = selected_real_candidate(source_candidate, [category])
            target = real_archive_table_target(config, client, category)
            target_page = verify_page_scope(client, record["target_page_id"], target.database_id, "Target")
            if not page_content_matches_candidate(target_page, target_candidate, config):
                raise WorkflowError(
                    "Final source removal stopped: current source content is not saved in the target archive database row. "
                    f"Review before removing: {record.get('source_url') or record.get('source_page_id')}"
                )
        return move
    target_database_id = config.get("target_databases", {}).get(category)
    if not target_database_id:
        raise WorkflowError(f"No target database configured for category: {category}")
    target_page = verify_page_scope(client, record["target_page_id"], target_database_id, "Target")
    if require_target_content_current:
        source_candidate = Candidate.from_notion_page(source_page, config)
        if source_candidate.category != category:
            raise WorkflowError(
                "Final source removal stopped: source category changed while pending removal. "
                f"Review before removing: {record.get('source_url') or record.get('source_page_id')}"
            )
        if not page_content_matches_candidate(target_page, source_candidate, config):
            raise WorkflowError(
                "Final source removal stopped: current source content is not saved in the target archive row. "
                f"Review before removing: {record.get('source_url') or record.get('source_page_id')}"
            )
    return move


def verify_dismissed_record_for_source_removal(
    config: dict[str, Any],
    client: NotionClient,
    record: dict[str, Any],
) -> None:
    source_page = verify_page_scope(client, record["source_page_id"], config["source_database_id"], "Source")
    props = source_page.get("properties", {})
    done_field = field_name(config, "done")
    if not read_checkbox(props.get(done_field, {})):
        raise WorkflowError(
            "Final source removal stopped: dismissed source row is not checked complete. "
            f"Review `{done_field}` before removing: {record.get('source_url') or record.get('source_page_id')}"
        )
    if config_mode(config) == REAL_MODE:
        current = real_candidate_from_notion_page(source_page, config)
    else:
        current = Candidate.from_notion_page(source_page, config)
    expected = record.get("fingerprint")
    if not expected:
        raise WorkflowError(
            "Final source removal stopped: dismissed record is missing source content evidence. "
            "Run `inspect` and review before removing source rows."
        )
    if expected != candidate_fingerprint(current):
        raise WorkflowError(
            "Final source removal stopped: dismissed source row changed after dismissal. "
            f"Review before removing: {record.get('source_url') or record.get('source_page_id')}"
        )


def record_from_ledger_pending_move(
    config: dict[str, Any],
    client: NotionClient,
    move: LedgerMove,
) -> dict[str, Any] | None:
    source_page_id = page_id_from_url(move.source_url)
    target_page_id = page_id_from_url(move.target_url)
    if not source_page_id or not target_page_id:
        return None
    target_database_id = config.get("target_databases", {}).get(move.category)
    if not target_database_id:
        return None
    source_page = client.retrieve_page(source_page_id)
    if source_page.get("archived"):
        return None
    expected_source_database_id = notion_id(config["source_database_id"])
    if page_database_id(source_page) != expected_source_database_id:
        raise WorkflowError(f"Source page is outside the expected database: {source_page_id}")
    fields = field_mapping(config)
    if not read_checkbox(source_page.get("properties", {}).get(fields["done"], {})):
        return None
    return {
        "session_id": move.session_id,
        "action": move.action,
        "operation_id": move.operation_id,
        "batch_id": move.batch_id,
        "batch_number": move.batch_number,
        "source_page_id": source_page_id,
        "source_url": move.source_url,
        "target_page_id": target_page_id,
        "target_url": move.target_url,
        "category": move.category,
        "name": read_title(source_page.get("properties", {}).get(fields["task"], {})),
        "ledger_page_id": move.page_id,
        "ledger_url": move.page_url,
    }


def ledger_pending_removal_records(
    config: dict[str, Any],
    client: NotionClient,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for move in ledger_pending_moves(config, client):
        record = record_from_ledger_pending_move(config, client, move)
        if record is not None:
            records.append(record)
    return records


def ledger_moved_source_ids(config: dict[str, Any], client: NotionClient, *, allow_stale_miss: bool = False) -> set[str]:
    cached = cached_ledger_effective_moves(config, getattr(client, "cache_path", Path(DEFAULT_CACHE)))
    if cached is not None:
        return cached[0]
    if allow_stale_miss:
        return set()
    moved, _ = ledger_effective_moves(config, client)
    return moved


def find_manual_archive_match(
    config: dict[str, Any],
    client: NotionClient,
    candidate: Candidate,
    context: RealArchiveLookupContext | None = None,
) -> ArchiveMatch | None:
    if config_mode(config) == REAL_MODE:
        categories = candidate.category_options or [candidate.category]
        match = None
        for category in categories:
            if category not in config.get("archive_tables", {}):
                continue
            target = real_archive_table_target(config, client, category, context)
            per_category = selected_real_candidate(candidate, [category])
            match = None
            for page in real_archive_database_pages(client, target.database_id, context):
                if not page.get("archived") and page_content_matches_candidate(page, per_category, config):
                    match = page
                    break
            if match is None:
                return None
        if match is None:
            return None
        return ArchiveMatch(
            target_page_id=match.get("id", notion_id(str(config["archive_tables_page_id"]))),
            target_url=match.get("url", real_archive_table_target_url(config, categories[0])),
        )
    target_database_id = config.get("target_databases", {}).get(candidate.category)
    if not target_database_id:
        return None
    for page in iter_source_pages(client, notion_id(target_database_id)):
        if page.get("archived"):
            continue
        if page_content_matches_candidate(page, candidate, config):
            return ArchiveMatch(target_page_id=page.get("id", ""), target_url=page.get("url", ""))
    return None


def record_manual_match(
    config: dict[str, Any],
    client: NotionClient,
    state: dict[str, Any],
    *,
    session_id: str,
    operation_id_value: str,
    batch_id: int,
    number: int,
    candidate: Candidate,
    match: ArchiveMatch,
) -> dict[str, Any]:
    record = {
        "action": "manual-match",
        "session_id": session_id,
        "operation_id": operation_id_value,
        "batch_id": batch_id,
        "batch_number": number,
        "source_page_id": candidate.source_page_id,
        "source_url": candidate.source_url,
        "target_page_id": match.target_page_id,
        "target_url": match.target_url,
        "category": candidate.category,
        "name": candidate.name,
    }
    state.setdefault("manual_matches", []).append(record)
    backup_record(
        state,
        {
            "action": "manual-match",
            "session_id": session_id,
            "operation_id": operation_id_value,
            "batch": batch_id,
            "number": number,
            "category": candidate.category,
            "name_hint": candidate.name,
            "source_page_id": candidate.source_page_id,
            "source_url": candidate.source_url,
            "target_page_id": match.target_page_id,
            "target_url": match.target_url,
        },
    )
    ledger_id = None if config_mode(config) == REAL_MODE else config.get("ledger_database_id")
    if ledger_id:
        ledger = client.create_manual_match_ledger_row(
            notion_id(ledger_id),
            session_id=session_id,
            operation_id=operation_id_value,
            batch_id=batch_id,
            number=number,
            candidate=candidate,
            target=match,
        )
        record["ledger_page_id"] = ledger.get("id")
        record["ledger_url"] = ledger.get("url", "")
    return record


def moved_operation_ids(state: dict[str, Any]) -> set[str]:
    records = effective_records(state.get("moved", []) + state.get("manual_matches", []), state)
    return {item["operation_id"] for item in records if item.get("operation_id")}


def effective_archive_operation_ids(state: dict[str, Any]) -> set[str]:
    return moved_operation_ids(state) | dismissed_operation_ids(state)


def effective_handled_batch_numbers(state: dict[str, Any]) -> set[int]:
    batch_id = int(state.get("batch_id", 0))
    batch = state.get("batch", [])
    handled: set[int] = set()
    records = effective_records(state.get("moved", []) + state.get("manual_matches", []), state)
    for item in records:
        if int(item.get("batch_id") or 0) == batch_id and item.get("batch_number"):
            handled.add(int(item["batch_number"]))
    for item in effective_records(state.get("dismissed", []), state):
        if int(item.get("batch_id") or 0) == batch_id and item.get("batch_number"):
            handled.add(int(item["batch_number"]))
    for item in state.get("skipped", []):
        if isinstance(item, dict):
            if int(item.get("batch_id") or 0) == batch_id and item.get("batch_number"):
                handled.add(int(item["batch_number"]))
        elif item:
            for number, batch_item in enumerate(batch, start=1):
                if notion_id(batch_item.get("source_page_id", "")) == notion_id(str(item)):
                    handled.add(number)
    return handled


def batch_complete(state: dict[str, Any]) -> bool:
    batch = state.get("batch", [])
    return bool(batch) and len(effective_handled_batch_numbers(state)) == len(batch)


def batch_record_by_number(records: list[dict[str, Any]], state: dict[str, Any], number: int) -> dict[str, Any] | None:
    batch_id = int(state.get("batch_id", 0))
    for item in effective_records(records, state):
        if int(item.get("batch_id") or 0) == batch_id and int(item.get("batch_number") or 0) == number:
            return item
    return None


def skipped_batch_numbers(state: dict[str, Any]) -> set[int]:
    numbers: set[int] = set()
    batch_id = int(state.get("batch_id", 0))
    batch = state.get("batch", [])
    for item in state.get("skipped", []):
        if isinstance(item, dict):
            if int(item.get("batch_id") or 0) == batch_id and item.get("batch_number"):
                numbers.add(int(item["batch_number"]))
        elif item:
            for number, batch_item in enumerate(batch, start=1):
                if notion_id(batch_item.get("source_page_id", "")) == notion_id(str(item)):
                    numbers.add(number)
    return numbers


def print_batch_complete_summary(state: dict[str, Any]) -> None:
    batch = [Candidate.from_state(item) for item in state.get("batch", [])]
    print("\nBatch complete. Review before moving on:")
    for number, candidate in enumerate(batch, start=1):
        moved = batch_record_by_number(state.get("moved", []), state, number)
        manual = batch_record_by_number(state.get("manual_matches", []), state, number)
        dismissed = batch_record_by_number(state.get("dismissed", []), state, number)
        if moved:
            status = f"moved -> {moved.get('category', candidate.category)}"
        elif manual:
            status = f"manual-match -> {manual.get('category', candidate.category)}"
        elif dismissed:
            status = "dismissed"
        elif number in skipped_batch_numbers(state):
            status = "skipped"
        else:
            status = "handled"
        print(f"  {number}. {status}: [{candidate.category}] {candidate.name}")
    print("You can still reply `undo N` for moved or dismissed items in this batch.")
    print("Reply `next` only when this summary looks right, or `done` to stop and review pending source cleanup.")


def print_batch_progress_prompt(state: dict[str, Any]) -> None:
    batch = state.get("batch", [])
    if not batch:
        return
    handled = effective_handled_batch_numbers(state)
    total = len(batch)
    remaining = [number for number in range(1, total + 1) if number not in handled]
    if remaining:
        print("\nBatch still has unhandled items: " + " ".join(str(number) for number in remaining))
        print("Reply with `ok N`, `dismiss N`, `skip N`, or `undo N` for a moved or dismissed item.")
        return
    print_batch_complete_summary(state)


def fetch_unchanged_candidate(client: NotionClient, snapshot: Candidate, config: dict[str, Any] | None = None) -> Candidate:
    page = client.retrieve_page(snapshot.source_page_id)
    if page.get("archived"):
        raise WorkflowError(f"Source row is archived/missing: {snapshot.source_page_id}")
    if config is not None and config_mode(config) == REAL_MODE:
        if not read_checkbox(page.get("properties", {}).get(field_name(config, "done"), {})):
            raise WorkflowError(
                "Source row is no longer checked complete. Run `preview` to refresh before moving: "
                f"[{snapshot.category}] {snapshot.name}"
            )
        current = real_candidate_from_notion_page(page, config)
    else:
        current = Candidate.from_notion_page(page, config)
    expected = snapshot.to_state().get("fingerprint")
    actual = candidate_fingerprint(current)
    if expected and expected != actual:
        raise WorkflowError(
            "Source row changed since the batch was shown. Run `next` to refresh before moving: "
            f"[{snapshot.category}] {snapshot.name}"
        )
    return current


def selected_real_candidate(candidate: Candidate, selected_categories: list[str]) -> Candidate:
    clone = Candidate(
        source_page_id=candidate.source_page_id,
        source_url=candidate.source_url,
        name=candidate.name,
        category=" + ".join(selected_categories),
        learnings=candidate.learnings,
        improvements=candidate.improvements,
        created_time=candidate.created_time,
        last_edited_time=candidate.last_edited_time,
        category_options=selected_categories,
    )
    return clone


def make_batch(config: dict[str, Any], state: dict[str, Any], limit: int) -> list[Candidate]:
    client = get_client(config.get("source_order", "bottom_first"))
    source_id = notion_id(config["source_database_id"])
    target_categories = set(config["target_databases"].keys())
    already_handled = (
        moved_source_ids(state)
        | manual_match_source_ids(state)
        | dismissed_source_ids(state)
        | skipped_source_ids(state)
        | ledger_moved_source_ids(config, client)
    )
    state["_empty_removed_this_batch"] = []
    state["_manual_matched_this_batch"] = []
    session_id = ensure_backup_session(state)
    batch_id = int(state.get("batch_id", 0))
    candidates: list[Candidate] = []
    page_size = max(10, limit * 3)
    for page_batch in iter_limited_source_page_batches(client, source_id, page_size):
        for page in page_batch:
            page_id = notion_id(page["id"])
            if page_id in already_handled:
                continue
            if empty_checked_candidate(page, config) is not None:
                remove_empty_checked_row(state, client, page, config)
                continue
            candidate = eligible_candidate(page, config)
            if candidate is None:
                continue
            if candidate.category not in target_categories:
                continue
            manual_match = find_manual_archive_match(config, client, candidate)
            if manual_match is not None:
                op_id = operation_id(session_id, candidate.source_page_id, 0)
                if op_id not in moved_operation_ids(state):
                    record = record_manual_match(
                        config,
                        client,
                        state,
                        session_id=session_id,
                        operation_id_value=op_id,
                        batch_id=batch_id,
                        number=0,
                        candidate=candidate,
                        match=manual_match,
                    )
                    state.setdefault("_manual_matched_this_batch", []).append(record)
                already_handled.add(page_id)
                continue
            candidates.append(candidate)
            if len(candidates) >= limit:
                return candidates
    return candidates


def make_preview_batch(config: dict[str, Any], state: dict[str, Any], limit: int) -> tuple[list[Candidate], list[Candidate], list[dict[str, Any]]]:
    client = get_client(config.get("source_order", "bottom_first"))
    source_id = notion_id(config["source_database_id"])
    if config_mode(config) == REAL_MODE:
        target_categories = set(config.get("project_categories", {}).keys())
    else:
        target_categories = set(config["target_databases"].keys())
    already_handled = (
        moved_source_ids(state)
        | manual_match_source_ids(state)
        | dismissed_source_ids(state)
        | skipped_source_ids(state)
        | ledger_moved_source_ids(config, client)
    )
    candidates: list[Candidate] = []
    empty_candidates: list[Candidate] = []
    manual_matches: list[dict[str, Any]] = []
    page_size = max(10, limit * 3)
    for page_batch in iter_limited_source_page_batches(client, source_id, page_size):
        for page in page_batch:
            page_id = notion_id(page["id"])
            if page_id in already_handled:
                continue
            if config_mode(config) == REAL_MODE:
                empty_candidate = real_empty_checked_candidate(page, config)
                candidate = real_eligible_candidate(page, config)
            else:
                empty_candidate = empty_checked_candidate(page, config)
                candidate = eligible_candidate(page, config)
            if empty_candidate is not None:
                empty_candidates.append(empty_candidate)
                already_handled.add(page_id)
                continue
            if candidate is None:
                continue
            if config_mode(config) == REAL_MODE:
                if not set(candidate.category_options or []).issubset(target_categories):
                    continue
                candidates.append(candidate)
                if len(candidates) >= limit:
                    return candidates, empty_candidates, manual_matches
                continue
            elif candidate.category not in target_categories:
                continue
            manual_match = find_manual_archive_match(config, client, candidate)
            if manual_match is not None:
                manual_matches.append(
                    {
                        "candidate": candidate,
                        "target_page_id": manual_match.target_page_id,
                        "target_url": manual_match.target_url,
                    }
                )
                already_handled.add(page_id)
                continue
            candidates.append(candidate)
            if len(candidates) >= limit:
                return candidates, empty_candidates, manual_matches
    return candidates, empty_candidates, manual_matches


def candidate_choices(index: int, candidate: Candidate, *, real_mode: bool) -> list[str]:
    if not real_mode:
        return [f"ok {index}", f"dismiss {index}", f"skip {index}"]
    options = candidate.category_options or []
    if len(options) <= 1:
        return [f"ok {index}", f"dismiss {index}", f"skip {index}"]
    choices = [f"ok {index} to {name}" for name in options]
    choices.append(f"ok {index} to all")
    choices.append(f"dismiss {index}")
    choices.append(f"skip {index}")
    return choices


def display_timestamp(value: str) -> str:
    if not value:
        return "unknown"
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M")


def print_candidate_timestamps(candidate: Candidate, *, indent: str = "   ") -> None:
    print(f"{indent}added: {display_timestamp(candidate.created_time)}")
    print(f"{indent}last edited: {display_timestamp(candidate.last_edited_time)}")


def print_preview_candidates(batch: list[Candidate], *, real_mode: bool, config: dict[str, Any] | None = None) -> None:
    if not batch:
        print("No eligible candidates found.")
        return
    for index, item in enumerate(batch, start=1):
        print(f"{index}. [{item.category}] {item.name}")
        print_candidate_timestamps(item)
        print(f"   {field_name(config, 'takeaway')}: {item.learnings or '(empty)'}")
        print(f"   {field_name(config, 'improvement')}: {item.improvements or '(empty)'}")
        print(f"   source: {item.source_url}")
        print("   Choices:")
        for choice in candidate_choices(index, item, real_mode=real_mode):
            print(f"     {choice}")


def print_preview(
    empty_candidates: list[Candidate],
    manual_matches: list[dict[str, Any]],
    batch: list[Candidate],
    *,
    real_mode: bool = False,
    config: dict[str, Any] | None = None,
) -> None:
    print("Preview only: no Notion rows were created, updated, archived, or removed.")
    if empty_candidates:
        print("\nWould remove empty completed rows when write mode is later approved:")
        for index, item in enumerate(empty_candidates, start=1):
            label = f"[{item.category}] " if item.category else ""
            print(f"  {index}. {label}{item.name or item.source_page_id}")
            print(f"     source: {item.source_url}")
            print("     Choices after done/confirm:")
            print(f"       keep empty {index}")
            print(f"       confirm remove empty {index}")
    if manual_matches:
        print("\nAlready manually archived; would record manual-match instead of copying:")
        for item in manual_matches:
            candidate = item["candidate"]
            print(f"  - [{candidate.category}] {candidate.name}")
            print(f"     source: {candidate.source_url}")
            print(f"     target: {item.get('target_url', '')}")
    if batch:
        print("\nWould present these candidates for approval:")
    print_preview_candidates(batch, real_mode=real_mode, config=config)
    if real_mode and batch:
        print("\nManual-match will be checked only when you approve with `ok`, to reduce Notion reads.")


def command_preview(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    options = organize_options(config, args)
    preflight(config)
    print_options(options)
    if config_mode(config) == REAL_MODE:
        print("Mode: real read-only")
    else:
        print("Mode: test preview")
    batch, empty_candidates, manual_matches = make_preview_batch(config, state, options["batch_size"])
    print_preview(empty_candidates, manual_matches, batch, real_mode=config_mode(config) == REAL_MODE, config=config)
    if config_mode(config) == REAL_MODE and getattr(args, "stage_real_trial", False):
        state["batch"] = [candidate.to_state() for candidate in batch]
        state["batch_id"] = int(state.get("batch_id", 0)) + 1
        state["session_options"] = options
        ensure_backup_session(state)
        backup_active_batch(state)
        save_json(args.state, state)
        print("\nStaged this preview locally as the active real batch.")


def print_batch(batch: list[Candidate], config: dict[str, Any] | None = None) -> None:
    if not batch:
        print("No eligible candidates found.")
        return
    for index, item in enumerate(batch, start=1):
        print(f"{index}. [{item.category}] {item.name}")
        print_candidate_timestamps(item)
        print(f"   {field_name(config, 'takeaway')}: {item.learnings or '(empty)'}")
        print(f"   {field_name(config, 'improvement')}: {item.improvements or '(empty)'}")
        print(f"   source: {item.source_url}")


def print_active_batch_resume(state: dict[str, Any], config: dict[str, Any] | None = None) -> None:
    if batch_complete(state):
        print_batch_complete_summary(state)
        return
    batch = [Candidate.from_state(item) for item in state.get("batch", [])]
    print("Continuing previous organize batch.")
    print(f"batch id: {state.get('batch_id', 0)}")
    print(f"active batch: {len(batch)}")
    print(f"moved pending: {len(state.get('moved', []))}")
    print(f"manual-match pending: {len(state.get('manual_matches', []))}")
    print(f"dismissed pending: {len(state.get('dismissed', []))}")
    print(f"skipped: {len(state.get('skipped', []))}")
    if batch:
        print()
        print_batch(batch, config)
        print("\nReply style:")
        print("  ok 1 3")
        print("  dismiss 1")
        print("  skip 2")
        print("  undo 1")
        print("  next --force  # replace this local batch")


def command_next(args: argparse.Namespace) -> None:
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    if state.get("batch") and not args.force and not batch_complete(state):
        config = load_config(args.config) if args.config.exists() else None
        print_active_batch_resume(state, config)
        return
    config = load_config(args.config)
    require_real_command_allowed(config, "next")
    options = session_options(state, config, args)
    preflight(config)
    ensure_backup_session(state)
    state["batch_id"] = int(state.get("batch_id", 0)) + 1
    if config_mode(config) == REAL_MODE:
        batch, empty_candidates, preview_manual_matches = make_preview_batch(config, state, options["batch_size"])
        empty_removed = [
            {"category": item.category, "name_hint": item.name, "source_page_id": item.source_page_id, "source_url": item.source_url}
            for item in empty_candidates
        ]
        manual_matched = [
            {
                "category": item["candidate"].category,
                "name": item["candidate"].name,
                "source_url": item["candidate"].source_url,
                "target_url": item.get("target_url", ""),
            }
            for item in preview_manual_matches
        ]
    else:
        batch = make_batch(config, state, options["batch_size"])
        empty_removed = state.pop("_empty_removed_this_batch", [])
        manual_matched = state.pop("_manual_matched_this_batch", [])
    state["batch"] = [item.to_state() for item in batch]
    state["preflight_ok"] = True
    state["preflight_scope"] = "next"
    if batch:
        backup_active_batch(state)
    save_json(args.state, state)
    print_options(options)
    print("Source order: bottom-first (oldest checked rows first)\n")
    if empty_removed:
        if config_mode(config) == REAL_MODE:
            print("Would remove empty completed rows after separate source-cleanup approval:")
        else:
            print("Removed empty completed rows reached in order:")
        for item in empty_removed:
            label = f"[{item.get('category')}] " if item.get("category") else ""
            print(f"  - {label}{item.get('name_hint') or item.get('source_page_id')}")
        print()
    if manual_matched:
        print("Already manually archived; skipped duplicate copy:")
        for item in manual_matched:
            print(f"  - [{item.get('category')}] {item.get('name')}")
            print(f"     source: {item.get('source_url', '')}")
            print(f"     target: {item.get('target_url', '')}")
        print()
    if config_mode(config) == REAL_MODE:
        print_preview_candidates(batch, real_mode=True, config=config)
        if batch:
            print("\nManual-match will be checked only when you approve with `ok`, to reduce Notion reads.")
    else:
        print_batch(batch, config)
    if not batch:
        pending = pending_moved_sources_still_in_source(config, get_client(config.get("source_order", "bottom_first")))
        if pending:
            print()
            print_pending_moved_sources(pending, config)
    if batch:
        print("\nReply style:")
        if config_mode(config) == REAL_MODE:
            example_categories = batch[0].category_options or sorted(config.get("project_categories", {}).keys())
            example_category = example_categories[0] if example_categories else "<category>"
            print(f"  ok 1 to {example_category}")
            print("  ok 1 to all")
        else:
            print("  ok 1 3")
        print("  dismiss 1")
        print("  skip 2")
        print("  undo 1")


def command_preflight(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    preflight(config, force=True)
    print("preflight ok")


def parse_numbers(values: list[str], max_number: int) -> list[int]:
    numbers: list[int] = []
    for value in values:
        try:
            number = int(value)
        except ValueError as exc:
            raise WorkflowError(f"Not a number: {value}") from exc
        if number < 1 or number > max_number:
            raise WorkflowError(f"Number out of range: {number}")
        numbers.append(number)
    return numbers


def resolve_requested_categories(tokens: list[str], options: list[str]) -> list[str]:
    remaining = list(tokens)
    resolved: list[str] = []
    ordered_options = sorted(options, key=lambda item: len(item.split()), reverse=True)
    while remaining:
        matched = None
        matched_parts = 0
        for option in ordered_options:
            parts = option.split()
            if remaining[: len(parts)] == parts:
                matched = option
                matched_parts = len(parts)
                break
        if matched is None:
            raise WorkflowError(f"Unknown category: {' '.join(remaining)}")
        resolved.append(matched)
        remaining = remaining[matched_parts:]
    return resolved


def parse_ok_items(values: list[str], batch: list[Candidate], *, real_mode: bool = False) -> list[dict[str, Any]]:
    if not real_mode:
        return [{"number": number, "selected_categories": []} for number in parse_numbers(values, len(batch))]
    if not values:
        raise WorkflowError("ok requires at least one item number.")
    try:
        to_index = values.index("to")
    except ValueError:
        to_index = -1
    if to_index == -1:
        numbers = parse_numbers(values, len(batch))
        parsed = []
        for number in numbers:
            candidate = batch[number - 1]
            options = candidate.category_options or []
            if len(options) > 1:
                raise WorkflowError(
                    f"Multi-category item requires a category choice. Use one of: "
                    + " | ".join(candidate_choices(number, candidate, real_mode=True))
                )
            parsed.append({"number": number, "selected_categories": options})
        return parsed
    numbers = parse_numbers(values[:to_index], len(batch))
    requested = values[to_index + 1 :]
    if not numbers or not requested:
        raise WorkflowError("Use `ok N to <category>` or `ok N to all`.")
    if len(numbers) != 1:
        raise WorkflowError("Use `ok N to ...` for one multi-category item at a time.")
    number = numbers[0]
    candidate = batch[number - 1]
    options = candidate.category_options or []
    if requested == ["all"]:
        return [{"number": number, "selected_categories": options}]
    try:
        selected = resolve_requested_categories(requested, options)
    except WorkflowError as exc:
        raise WorkflowError(
            f"{exc}. Use one of: "
            + " | ".join(candidate_choices(number, candidate, real_mode=True))
        ) from exc
    return [{"number": number, "selected_categories": selected}]


def command_ok(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    real_mode = config_mode(config) == REAL_MODE
    if real_mode:
        require_real_command_allowed(config, "ok")
    else:
        require_writable_mode(config, "ok")
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    batch = [Candidate.from_state(item) for item in state.get("batch", [])]
    if not batch:
        raise WorkflowError("No active batch. Run `next` first.")
    selected_items = parse_ok_items(args.items, batch, real_mode=real_mode)
    options = session_options(state, config)
    session_id = ensure_backup_session(state)
    client = get_client()
    moved_this_run = 0
    limit = options["move_limit"]
    current_count = int(state.get("moved_count", 0))
    current_batch_id = int(state.get("batch_id", 0))
    already_moved = moved_source_ids(state) | manual_match_source_ids(state) | dismissed_source_ids(state)
    already_ops = effective_archive_operation_ids(state)
    ledger_moved, ledger_ops = ledger_effective_moves(config, client)
    real_archive_context = RealArchiveLookupContext() if real_mode else None
    for selected_item in selected_items:
        number = int(selected_item["number"])
        selected_categories = list(selected_item.get("selected_categories") or [])
        if current_count + moved_this_run >= limit:
            print(f"Move limit reached: {current_count + moved_this_run} / {limit}")
            break
        snapshot = batch[number - 1]
        op_id = next_operation_id(state, session_id, snapshot.source_page_id, number)
        source_id = notion_id(snapshot.source_page_id)
        if op_id in already_ops or source_id in already_moved:
            print(f"ok {number}: already moved, skipping duplicate [{snapshot.category}] {snapshot.name}")
            continue
        if op_id in ledger_ops or source_id in ledger_moved:
            print(f"ok {number}: ledger already has an effective moved record, skipping duplicate [{snapshot.category}] {snapshot.name}")
            continue
        candidate = fetch_unchanged_candidate(client, snapshot, config)
        if real_mode:
            if not selected_categories:
                selected_categories = candidate.category_options or []
            if not selected_categories:
                raise WorkflowError("Real mode item has no selected project category.")
            if not set(selected_categories).issubset(set(candidate.category_options or [])):
                raise WorkflowError(
                    "Selected category no longer matches the source row relation. Run `preview` to refresh before moving."
                )
            candidate = selected_real_candidate(candidate, selected_categories)
        if notion_id(candidate.source_page_id) in already_moved:
            print(f"ok {number}: already moved, skipping duplicate [{candidate.category}] {candidate.name}")
            continue
        if notion_id(candidate.source_page_id) in ledger_moved:
            print(f"ok {number}: ledger already has source moved, skipping duplicate [{candidate.category}] {candidate.name}")
            continue
        if real_mode:
            target_candidates = [selected_real_candidate(candidate, [category]) for category in selected_categories]
        else:
            target_candidates = [candidate]
        wrote_or_matched = False
        for target_candidate in target_candidates:
            if real_mode:
                created = create_real_archive_table_row(config, client, target_candidate, real_archive_context)
            else:
                target_id = config["target_databases"].get(target_candidate.category)
                if not target_id:
                    raise WorkflowError(f"No target database configured for category: {target_candidate.category}")
                manual_match = find_manual_archive_match(config, client, target_candidate)
                if manual_match is not None:
                    record = record_manual_match(
                        config,
                        client,
                        state,
                        session_id=session_id,
                        operation_id_value=op_id,
                        batch_id=current_batch_id,
                        number=number,
                        candidate=target_candidate,
                        match=manual_match,
                    )
                    save_json(args.state, state)
                    print(f"ok {number}: already manually archived, skipped duplicate [{target_candidate.category}] {target_candidate.name}")
                    print(f"   target: {record.get('target_url', '')}")
                    wrote_or_matched = True
                    continue
                created = create_archive_row_with_config(client, notion_id(target_id), target_candidate, config)
            if created.get("manual_match"):
                match = ArchiveMatch(target_page_id=created["id"], target_url=created.get("url", ""))
                record = record_manual_match(
                    config,
                    client,
                    state,
                    session_id=session_id,
                    operation_id_value=op_id,
                    batch_id=current_batch_id,
                    number=number,
                    candidate=target_candidate,
                    match=match,
                )
                save_json(args.state, state)
                print(f"ok {number}: already manually archived, skipped duplicate [{target_candidate.category}] {target_candidate.name}")
                print(f"   target: {record.get('target_url', '')}")
                wrote_or_matched = True
                continue
            record = {
                "session_id": session_id,
                "operation_id": op_id,
                "batch_id": current_batch_id,
                "batch_number": number,
                "source_page_id": target_candidate.source_page_id,
                "source_url": target_candidate.source_url,
                "target_page_id": created["id"],
                "target_url": created.get("url", ""),
                "category": target_candidate.category,
                "name": target_candidate.name,
            }
            state.setdefault("moved", []).append(record)
            backup_record(
                state,
                {
                    "action": "moved",
                    "session_id": session_id,
                    "operation_id": op_id,
                    "batch": current_batch_id,
                    "number": number,
                    "category": target_candidate.category,
                    "name_hint": target_candidate.name,
                    "source_page_id": target_candidate.source_page_id,
                    "source_url": target_candidate.source_url,
                    "target_page_id": created["id"],
                    "target_url": created.get("url", ""),
                },
            )
            save_json(args.state, state)
            safety_parent_id = None if real_mode else config.get("safety_log_parent_page_id")
            if safety_parent_id:
                safety_log = client.create_safety_log_page(
                    notion_id(safety_parent_id),
                    session_id=session_id,
                    operation_id=op_id,
                    number=number,
                    candidate=target_candidate,
                    selected_categories=[target_candidate.category],
                    target=created,
                )
                record["safety_log_page_id"] = safety_log.get("id")
                record["safety_log_url"] = safety_log.get("url", "")
                save_json(args.state, state)
            ledger_id = None if real_mode else config.get("ledger_database_id")
            if ledger_id:
                ledger = client.create_ledger_row(
                    notion_id(ledger_id),
                    session_id=session_id,
                    operation_id=op_id,
                    batch_id=current_batch_id,
                    number=number,
                    candidate=target_candidate,
                    target=created,
                )
                record["ledger_page_id"] = ledger.get("id")
                record["ledger_url"] = ledger.get("url", "")
                save_json(args.state, state)
            print(f"ok {number}: moved [{target_candidate.category}] {target_candidate.name}")
            print(f"   target: {created.get('url', '')}")
            wrote_or_matched = True
        if not wrote_or_matched:
            continue
        moved_this_run += 1
        already_moved.add(notion_id(candidate.source_page_id))
        already_ops.add(op_id)
        ledger_moved.add(notion_id(candidate.source_page_id))
        ledger_ops.add(op_id)
    state["moved_count"] = int(state.get("moved_count", 0)) + moved_this_run
    save_json(args.state, state)
    if state["moved_count"] >= limit:
        print(f"\nMove limit reached: {state['moved_count']} / {limit}")
    print_batch_progress_prompt(state)


def command_skip(args: argparse.Namespace) -> None:
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    batch = [Candidate.from_state(item) for item in state.get("batch", [])]
    if not batch:
        raise WorkflowError("No active batch. Run `next` first.")
    selected = parse_numbers(args.items, len(batch))
    ensure_backup_session(state)
    for number in selected:
        candidate = batch[number - 1]
        state.setdefault("skipped", []).append(
            {
                "batch_id": int(state.get("batch_id", 0)),
                "batch_number": number,
                "source_page_id": candidate.source_page_id,
            }
        )
        backup_record(
            state,
            {
                "action": "skipped",
                "batch": int(state.get("batch_id", 0)),
                "number": number,
                "category": candidate.category,
                "name_hint": candidate.name,
                "source_page_id": candidate.source_page_id,
                "source_url": candidate.source_url,
            },
        )
        print(f"skip {number}: [{candidate.category}] {candidate.name}")
    save_json(args.state, state)
    print_batch_progress_prompt(state)


def command_dismiss(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    require_real_command_allowed(config, "dismiss")
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    batch = [Candidate.from_state(item) for item in state.get("batch", [])]
    if not batch:
        raise WorkflowError("No active batch. Run `next` first.")
    selected = parse_numbers(args.items, len(batch))
    session_id = ensure_backup_session(state)
    client = get_client()
    current_batch_id = int(state.get("batch_id", 0))
    already_handled = moved_source_ids(state) | manual_match_source_ids(state) | dismissed_source_ids(state)
    already_ops = effective_archive_operation_ids(state)
    skipped_sources = skipped_source_ids(state)
    for number in selected:
        snapshot = batch[number - 1]
        op_id = next_operation_id(state, session_id, snapshot.source_page_id, number)
        source_id = notion_id(snapshot.source_page_id)
        if op_id in already_ops or source_id in already_handled:
            print(f"dismiss {number}: already handled [{snapshot.category}] {snapshot.name}")
            continue
        if source_id in skipped_sources:
            print(f"dismiss {number}: already skipped [{snapshot.category}] {snapshot.name}")
            continue
        candidate = fetch_unchanged_candidate(client, snapshot, config)
        record = {
            "action": "dismissed",
            "session_id": session_id,
            "operation_id": op_id,
            "batch_id": current_batch_id,
            "batch_number": number,
            "source_page_id": candidate.source_page_id,
            "source_url": candidate.source_url,
            "category": candidate.category,
            "name": candidate.name,
            "fingerprint": candidate_fingerprint(candidate),
        }
        state.setdefault("dismissed", []).append(record)
        backup_record(
            state,
            {
                "action": "dismissed",
                "session_id": session_id,
                "operation_id": op_id,
                "batch": current_batch_id,
                "number": number,
                "category": candidate.category,
                "name_hint": candidate.name,
                "source_page_id": candidate.source_page_id,
                "source_url": candidate.source_url,
            },
        )
        print(f"dismiss {number}: [{candidate.category}] {candidate.name}")
        already_handled.add(source_id)
        already_ops.add(op_id)
    save_json(args.state, state)
    print_batch_progress_prompt(state)


def command_undo(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    require_real_command_allowed(config, "undo")
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    moved = state.get("moved", [])
    dismissed = state.get("dismissed", [])
    if not moved and not dismissed:
        raise WorkflowError("No moved or dismissed rows to undo.")
    selected = set(parse_numbers(args.items, max(1, len(state.get("batch", [])))))
    current_batch_id = int(state.get("batch_id", 0))
    client = None
    remaining = []
    remaining_dismissed = []
    undone = 0
    undone_moved = 0
    for record in moved:
        if (
            record.get("batch_id") == current_batch_id
            and record.get("batch_number") in selected
            and record.get("target_page_id")
        ):
            if client is None:
                client = get_client()
            verify_moved_record_for_destructive_action(config, client, record, allowed_actions={"moved"})
            client.archive_page(record["target_page_id"])
            ledger_id = None if config_mode(config) == REAL_MODE else config.get("ledger_database_id")
            if ledger_id:
                ledger = client.create_undo_ledger_row(
                    notion_id(ledger_id),
                    record=record,
                    batch_id=current_batch_id,
                    number=int(record.get("batch_number") or 0),
                )
                record["undo_ledger_page_id"] = ledger.get("id")
                record["undo_ledger_url"] = ledger.get("url", "")
            backup_record(
                state,
                {
                    "action": "undone",
                    "session_id": record.get("session_id"),
                    "operation_id": record.get("operation_id"),
                    "batch": current_batch_id,
                    "number": record.get("batch_number"),
                    "category": record.get("category"),
                    "name_hint": record.get("name"),
                    "source_page_id": record.get("source_page_id"),
                    "source_url": record.get("source_url"),
                    "target_page_id": record.get("target_page_id"),
                    "target_url": record.get("target_url"),
                },
            )
            state.setdefault("undone", []).append(record)
            undone += 1
            undone_moved += 1
            print(f"undo {record['batch_number']}: archived {record.get('target_url', '')}")
        else:
            remaining.append(record)
    for record in dismissed:
        if record.get("batch_id") == current_batch_id and record.get("batch_number") in selected:
            backup_record(
                state,
                {
                    "action": "undone",
                    "session_id": record.get("session_id"),
                    "operation_id": record.get("operation_id"),
                    "batch": current_batch_id,
                    "number": record.get("batch_number"),
                    "category": record.get("category"),
                    "name_hint": record.get("name"),
                    "source_page_id": record.get("source_page_id"),
                    "source_url": record.get("source_url"),
                },
            )
            state.setdefault("undone", []).append(record)
            undone += 1
            print(f"undo {record['batch_number']}: dismissed item restored for handling")
        else:
            remaining_dismissed.append(record)
    if undone == 0:
        raise WorkflowError("No matching moved or dismissed rows found for undo.")
    state["moved"] = remaining
    state["dismissed"] = remaining_dismissed
    state["moved_count"] = max(0, int(state.get("moved_count", 0)) - undone_moved)
    save_json(args.state, state)
    print_batch_progress_prompt(state)


def command_save(args: argparse.Namespace) -> None:
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    old_session_id = state.get("backup_session_id") or state.get("session_id")
    if old_session_id:
        finish_backup_session(state, "saved")
        state.setdefault("saved_sessions", []).append(old_session_id)
    new_session_id = begin_new_backup_session(state)
    if state.get("batch"):
        backup_active_batch(state)
    state["moved_count"] = 0
    save_json(args.state, state)
    print(f"saved session: {old_session_id or '(none)'}")
    print(f"new session: {new_session_id}")
    if state.get("batch"):
        print("active batch preserved; you can continue with ok/dismiss/skip/undo")


def command_done(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    require_real_command_allowed(config, "done")
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    finish_backup_session(state, "done")
    state["done"] = True
    state["awaiting_done_confirm"] = bool(state.get("moved") or state.get("manual_matches") or state.get("dismissed"))
    save_json(args.state, state)
    moved = state.get("moved", [])
    manual_matches = state.get("manual_matches", [])
    dismissed = state.get("dismissed", [])
    skipped = state.get("skipped", [])
    print("done")
    print("Change summary:")
    print(f"categorized: {len(moved)}")
    if moved:
        for record in moved:
            print(f"  - [{record.get('category')}] {record.get('name')}")
    print(f"manually matched: {len(manual_matches)}")
    if manual_matches:
        for record in manual_matches:
            print(f"  - [{record.get('category')}] {record.get('name')}")
    print(f"dismissed without archive: {len(dismissed)}")
    if dismissed:
        for record in dismissed:
            print(f"  - [{record.get('category')}] {record.get('name')}")
    empty_removed = state.get("empty_removed", [])
    print(f"deleted empty completed rows: {len(empty_removed)}")
    if empty_removed:
        for record in empty_removed:
            label = f"[{record.get('category')}] " if record.get("category") else ""
            print(f"  - {label}{record.get('name_hint') or record.get('source_page_id')}")
    print(f"skipped: {len(skipped)}")
    pending = [] if config_mode(config) == REAL_MODE else pending_moved_sources_still_in_source(config, get_client(config.get("source_order", "bottom_first")))
    if pending:
        print()
        print_pending_moved_sources(pending, config)
    if state.get("awaiting_done_confirm"):
        print("\nConfirm today's changes? Reply `confirm` to finalize checked pending source removals, including dismissed rows.")
    else:
        print("\nNo source rows are waiting for final source removal.")


def command_confirm(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    require_real_command_allowed(config, "confirm")
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    if not state.get("awaiting_done_confirm"):
        print("No done confirmation is waiting.")
        return
    removed = finalize_source_removal(config, state, args.state)
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    state["awaiting_done_confirm"] = bool(state.get("moved") or state.get("manual_matches") or state.get("dismissed"))
    save_json(args.state, state)
    print("confirmed")
    if state["awaiting_done_confirm"]:
        print("pending rows still need review; fix them and run `confirm` again")
    if removed:
        print("finalized removals:")
        for record in removed:
            print(f"  - [{record.get('category')}] {record.get('name')}")


def finalize_source_removal(config: dict[str, Any], state: dict[str, Any], state_path: Path) -> list[dict[str, Any]]:
    moved = [dict(record, _state_bucket="moved") for record in state.get("moved", [])]
    moved.extend(dict(record, _state_bucket="manual_matches") for record in state.get("manual_matches", []))
    moved.extend(dict(record, _state_bucket="dismissed") for record in state.get("dismissed", []))
    client = get_client()
    if not moved:
        moved = ledger_pending_removal_records(config, client)
        if not moved:
            print("No source rows pending removal.")
            return []
        moved = [dict(record, _state_bucket="_ledger") for record in moved]
    incomplete = [
        record
        for record in moved
        if record.get("_state_bucket") == "dismissed"
        and not (record.get("source_page_id") and record.get("operation_id") and record.get("fingerprint"))
    ]
    incomplete.extend(
        record
        for record in moved
        if record.get("_state_bucket") != "dismissed"
        and not (
            record.get("source_page_id")
            and record.get("target_page_id")
            and record.get("operation_id")
            and (config_mode(config) == REAL_MODE or record.get("ledger_page_id"))
        )
    )
    if incomplete:
        evidence = "source/operation/fingerprint evidence" if any(record.get("_state_bucket") == "dismissed" for record in incomplete) else "source/target/operation evidence"
        if config_mode(config) != REAL_MODE and evidence == "source/target/operation evidence":
            evidence = "source/target/operation/ledger evidence"
        raise WorkflowError(
            f"Final source removal stopped: moved records are missing {evidence}. "
            "Run `inspect` and review before removing source rows."
        )
    remaining_moved = []
    remaining_manual_matches = []
    remaining_dismissed = []
    removed = []
    needs_review = []
    attempted_ops = {record.get("operation_id") for record in moved if record.get("operation_id")}
    undone_ops = {record.get("operation_id") for record in state.get("undone", []) if record.get("operation_id")}
    undone_sources = {notion_id(record.get("source_page_id", "")) for record in state.get("undone", []) if record.get("source_page_id")}
    skipped_sources = {notion_id(record.get("source_page_id", "")) for record in state.get("skipped", []) if record.get("source_page_id")}
    for record in moved:
        source_page_id = record.get("source_page_id")
        if not source_page_id:
            needs_review.append(dict(record, reason="missing source page id"))
            continue
        source_compact = notion_id(source_page_id)
        kept = {key: value for key, value in record.items() if key != "_state_bucket"}
        if record.get("operation_id") in undone_ops or source_compact in undone_sources:
            kept["needs_review"] = "Final source removal stopped: this record was undone."
            needs_review.append(kept)
            if record.get("_state_bucket") == "manual_matches":
                remaining_manual_matches.append(kept)
            elif record.get("_state_bucket") == "dismissed":
                remaining_dismissed.append(kept)
            elif record.get("_state_bucket") == "moved":
                remaining_moved.append(kept)
            print(f"needs review before source removal: {record.get('source_url') or source_page_id}")
            print("  reason: Final source removal stopped: this record was undone.")
            continue
        if source_compact in skipped_sources:
            kept["needs_review"] = "Final source removal stopped: this source row was skipped."
            needs_review.append(kept)
            if record.get("_state_bucket") == "manual_matches":
                remaining_manual_matches.append(kept)
            elif record.get("_state_bucket") == "dismissed":
                remaining_dismissed.append(kept)
            elif record.get("_state_bucket") == "moved":
                remaining_moved.append(kept)
            print(f"needs review before source removal: {record.get('source_url') or source_page_id}")
            print("  reason: Final source removal stopped: this source row was skipped.")
            continue
        try:
            if record.get("_state_bucket") == "dismissed":
                verify_dismissed_record_for_source_removal(config, client, record)
                ledger_move = LedgerMove(
                    page_id="",
                    page_url="",
                    action="dismissed",
                    operation_id=record.get("operation_id", ""),
                    session_id=record.get("session_id", ""),
                    source_url=record.get("source_url", ""),
                    target_url="",
                    category=record.get("category", ""),
                    batch_id=int(record.get("batch_id") or 0),
                    batch_number=int(record.get("batch_number") or 0),
                )
            else:
                ledger_move = verify_moved_record_for_destructive_action(
                    config,
                    client,
                    record,
                    require_source_completed=True,
                    require_target_content_current=True,
                    allowed_actions={"moved", "manual-match"},
                )
        except WorkflowError as exc:
            kept = {key: value for key, value in record.items() if key != "_state_bucket"}
            kept["needs_review"] = str(exc)
            needs_review.append(kept)
            if record.get("_state_bucket") == "manual_matches":
                remaining_manual_matches.append(kept)
            elif record.get("_state_bucket") == "dismissed":
                remaining_dismissed.append(kept)
            elif record.get("_state_bucket") == "moved":
                remaining_moved.append(kept)
            print(f"needs review before source removal: {record.get('source_url') or source_page_id}")
            print(f"  reason: {exc}")
            continue
        client.archive_page(source_page_id)
        if config_mode(config) != REAL_MODE and ledger_move.page_id:
            client.update_ledger_stability(ledger_move.page_id, "removed")
        backup_record(
            state,
            {
                "action": "removed",
                "session_id": record.get("session_id"),
                "operation_id": record.get("operation_id"),
                "batch": record.get("batch_id"),
                "number": record.get("batch_number"),
                "category": record.get("category"),
                "name_hint": record.get("name"),
                "source_page_id": source_page_id,
                "source_url": record.get("source_url"),
                "target_page_id": record.get("target_page_id"),
                "target_url": record.get("target_url"),
                "ledger_page_id": ledger_move.page_id,
                "ledger_url": ledger_move.page_url,
            },
        )
        record["ledger_page_id"] = ledger_move.page_id
        record["ledger_url"] = ledger_move.page_url
        record["removed"] = True
        removed.append({key: value for key, value in record.items() if key != "_state_bucket"})
    state["moved"] = remaining_moved
    state["manual_matches"] = remaining_manual_matches
    state["dismissed"] = remaining_dismissed
    if attempted_ops:
        state["needs_review"] = [
            record
            for record in state.get("needs_review", [])
            if record.get("operation_id") not in attempted_ops
        ]
    if needs_review:
        state.setdefault("needs_review", []).extend(needs_review)
    state.setdefault("removed", []).extend(removed)
    save_json(state_path, state)
    print(f"removed source rows: {len(removed)}")
    for record in removed:
        print(f"  {record.get('batch_number')}: {record.get('source_url', '')}")
    return removed


def command_remove_sources(args: argparse.Namespace) -> None:
    if args.confirm != "REMOVE_SOURCES":
        raise WorkflowError("Final source removal requires `--confirm REMOVE_SOURCES`.")
    config = load_config(args.config)
    require_real_command_allowed(config, "remove-sources")
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    if state.get("dismissed"):
        raise WorkflowError(
            "Dismissed source rows require the normal `done` -> `confirm` cleanup path. "
            "Run `done`, review the summary, then reply `confirm`."
        )
    finalize_source_removal(config, state, args.state)


def pending_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    records = [dict(record, _kind="moved") for record in state.get("moved", [])]
    records.extend(dict(record, _kind="manual-match") for record in state.get("manual_matches", []))
    records.extend(dict(record, _kind="dismissed") for record in state.get("dismissed", []))
    return records


def command_check(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    preflight(config)
    print("schema: ok")
    print(f"active batch: {len(state.get('batch', []))}")
    print(f"pending moved: {len(state.get('moved', []))}")
    print(f"pending manual-match: {len(state.get('manual_matches', []))}")
    print(f"pending dismissed: {len(state.get('dismissed', []))}")
    print(f"needs review: {len(state.get('needs_review', []))}")
    print(f"awaiting confirm: {bool(state.get('awaiting_done_confirm'))}")

    records = pending_records(state)
    if not records:
        print("pending safety: ok")
        return

    client = get_client()
    blocked = 0
    for record in records:
        try:
            if record.get("_kind") == "dismissed":
                verify_dismissed_record_for_source_removal(config, client, record)
            else:
                verify_moved_record_for_destructive_action(
                    config,
                    client,
                    record,
                    require_source_completed=True,
                    require_target_content_current=True,
                    allowed_actions={"moved", "manual-match"},
                )
        except WorkflowError as exc:
            blocked += 1
            print(f"pending safety: needs-review [{record.get('category')}] {record.get('name')}")
            print(f"  reason: {exc}")
    if blocked == 0:
        print("pending safety: ok")
    else:
        print(f"pending safety: {blocked} needs-review")


def command_discard(args: argparse.Namespace) -> None:
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    if not state.get("batch"):
        print("No active batch to discard.")
        return
    if (state.get("moved") or state.get("manual_matches") or state.get("dismissed")) and not args.force:
        print("Active batch has rows pending-removal.")
        print("Run `status` or `done` first, or use `discard --force` if you only want to clear the local active batch.")
        return
    discarded = len(state.get("batch", []))
    state["batch"] = []
    state["discarded_batch_id"] = state.get("batch_id", 0)
    backup_record(
        state,
        {
            "action": "discarded",
            "batch": int(state.get("batch_id", 0)),
            "status": "local-active-batch-cleared",
            "name_hint": f"discarded {discarded} local batch items",
        },
    )
    save_json(args.state, state)
    print(f"discarded local active batch: {discarded}")
    print("No Notion rows were modified.")


def command_status(args: argparse.Namespace) -> None:
    state = load_json(args.state, {"batch": [], "moved": [], "skipped": []})
    print(f"session: {state.get('session_id') or state.get('backup_session_id') or '(none)'}")
    print(f"active batch: {len(state.get('batch', []))}")
    print(f"moved this session: {state.get('moved_count', 0)}")
    print(f"moved pending-removal: {len(state.get('moved', []))}")
    print(f"manual-match pending-removal: {len(state.get('manual_matches', []))}")
    print(f"dismissed pending-removal: {len(state.get('dismissed', []))}")
    print(f"skipped total: {len(state.get('skipped', []))}")
    print(f"undone total: {len(state.get('undone', []))}")
    print(f"removed total: {len(state.get('removed', []))}")
    print(f"needs review total: {len(state.get('needs_review', []))}")
    print(f"awaiting done confirm: {bool(state.get('awaiting_done_confirm'))}")
    if isinstance(state.get("session_options"), dict):
        print_options(state["session_options"])
    if state.get("batch"):
        print()
        print_batch([Candidate.from_state(item) for item in state["batch"]], config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="External CLI fallback for the approval-first Notion to-do organizer.")
    parser.add_argument("--config", type=Path, default=Path("config.test.json"))
    parser.add_argument("--state", type=Path, default=Path(DEFAULT_STATE))
    sub = parser.add_subparsers(dest="command", required=True)

    next_parser = sub.add_parser("next", help="Stage and print the next batch.")
    next_parser.add_argument("--batch-size", type=int, help="Override config batch_size for this session.")
    next_parser.add_argument("--move-limit", type=int, help="Override config move_limit for this session.")
    next_parser.add_argument("--limit", type=int, dest="batch_size", help=argparse.SUPPRESS)
    next_parser.add_argument("--force", action="store_true", help="Replace an existing local active batch.")
    next_parser.set_defaults(func=command_next)

    preflight_parser = sub.add_parser("preflight", help="Verify configured database schemas.")
    preflight_parser.set_defaults(func=command_preflight)

    preview_parser = sub.add_parser("preview", help="Read-only preview of the next candidate batch; never modifies Notion rows.")
    preview_parser.add_argument("--batch-size", type=int, help="Override config batch_size for this preview.")
    preview_parser.add_argument("--move-limit", type=int, help="Override config move_limit for this preview.")
    preview_parser.add_argument("--limit", type=int, dest="batch_size", help=argparse.SUPPRESS)
    preview_parser.add_argument(
        "--stage-real-trial",
        action="store_true",
        help="In real mode, save the read-only preview to local state as the active batch.",
    )
    preview_parser.set_defaults(func=command_preview)

    check_parser = sub.add_parser("check", help="Run a lightweight stability check without modifying Notion rows.")
    check_parser.set_defaults(func=command_check)

    ok_parser = sub.add_parser("ok", help="Move approved batch rows.")
    ok_parser.add_argument("items", nargs="+")
    ok_parser.add_argument(
        "--real-write-confirm",
        help=argparse.SUPPRESS,
    )
    ok_parser.set_defaults(func=command_ok)

    dismiss_parser = sub.add_parser("dismiss", help="Mark batch rows for source removal without archiving.")
    dismiss_parser.add_argument("items", nargs="+")
    dismiss_parser.set_defaults(func=command_dismiss)

    skip_parser = sub.add_parser("skip", help="Mark batch rows as skipped.")
    skip_parser.add_argument("items", nargs="+")
    skip_parser.set_defaults(func=command_skip)

    undo_parser = sub.add_parser("undo", help="Undo moved batch rows by archiving created target rows.")
    undo_parser.add_argument("items", nargs="+")
    undo_parser.set_defaults(func=command_undo)

    save_parser = sub.add_parser("save", help="Save current progress and start a fresh backup session without ending the active batch.")
    save_parser.set_defaults(func=command_save)

    done_parser = sub.add_parser("done", help="Finish the current organize loop and show the end summary.")
    done_parser.set_defaults(func=command_done)

    confirm_parser = sub.add_parser("confirm", help="Confirm the done summary and finalize checked pending source removals.")
    confirm_parser.set_defaults(func=command_confirm)

    remove_parser = sub.add_parser("remove-sources", help="Final source-row removal after done; requires explicit confirmation.")
    remove_parser.add_argument("--confirm", required=True)
    remove_parser.set_defaults(func=command_remove_sources)

    discard_parser = sub.add_parser("discard", help="Clear only the local active batch; never modifies Notion rows.")
    discard_parser.add_argument("--force", action="store_true", help="Allow discarding even when moved rows are pending.")
    discard_parser.set_defaults(func=command_discard)

    inspect_parser = sub.add_parser("inspect", help="Alias for status; inspect current local state.")
    inspect_parser.set_defaults(func=command_status)

    status_parser = sub.add_parser("status", help="Show current state.")
    status_parser.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
