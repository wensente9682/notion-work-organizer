"""Read-only Total and Organize smoke from one in-memory New System profile."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from typing import Mapping

from new_system_blueprint import ARCHIVE_PROPERTIES, SOURCE_PROPERTIES
from new_system_onboarding import profile_is_valid
from notion_todo_workflow import real_eligible_candidate
from total import TotalItem, total


@dataclass(frozen=True)
class NewSystemReadinessReport:
    status: str
    category_totals: Mapping[str, str]
    grand_total: str | None
    preview_count: int | None
    phases: tuple[str, ...]


def _failed() -> NewSystemReadinessReport:
    return NewSystemReadinessReport("not-ready", {}, None, None, ())


def _has_required_properties(actual: object, required: Mapping[str, str]) -> bool:
    return type(actual) is dict and all(
        actual.get(name) == property_type
        for name, property_type in required.items()
    )


def _row_to_item(row: dict[str, object]) -> TotalItem:
    done = row.get("Done")
    category = row.get("Category")
    blocks = row.get("Time Blocks")
    work_date = row.get("Work Date")
    if done not in {"__YES__", "__NO__"}:
        raise ValueError
    if category is not None and type(category) is not str:
        raise ValueError
    if blocks is not None and type(blocks) is not str:
        raise ValueError
    if work_date is not None and type(work_date) is not str:
        raise ValueError
    return TotalItem(
        date_anchor=date.fromisoformat(work_date[:10]) if work_date else None,
        done=done == "__YES__",
        categories=(category,) if category else (),
        timeboxing=blocks or "",
    )


def _row_to_page(row: dict[str, object]) -> dict[str, object]:
    item = _row_to_item(row)
    text = lambda value: [{"plain_text": value}] if type(value) is str and value else []
    category = item.categories[0] if item.categories else None
    return {
        "id": row.get("url", ""),
        "url": row.get("url", ""),
        "created_time": "",
        "last_edited_time": "",
        "properties": {
            "Task": {"type": "title", "title": text(row.get("Task"))},
            "Done": {"type": "checkbox", "checkbox": item.done},
            "Category": {"type": "select", "select": {"name": category} if category else None},
            "Takeaway": {"type": "rich_text", "rich_text": text(row.get("Takeaway"))},
            "Improvement": {"type": "rich_text", "rich_text": text(row.get("Improvement"))},
        },
    }


def verify_new_system_readiness(
    profile_content: object,
    *,
    source_schema: object,
    archive_schemas: object,
    source_rows: object,
    target_month: object,
) -> NewSystemReadinessReport:
    """Consume bounded connector read facts; this function has no I/O capability."""
    try:
        if type(profile_content) is not bytes or type(target_month) is not str:
            return _failed()
        profile = json.loads(profile_content)
        if not profile_is_valid(profile):
            return _failed()
        if not _has_required_properties(source_schema, SOURCE_PROPERTIES) or type(archive_schemas) is not dict:
            return _failed()
        categories = tuple(profile["source"]["categories"])
        if set(archive_schemas) != set(categories) or any(
            not _has_required_properties(archive_schemas[category], ARCHIVE_PROPERTIES)
            for category in categories
        ):
            return _failed()
        if type(source_rows) is not list or len(source_rows) > 100 or any(type(row) is not dict for row in source_rows):
            return _failed()
        month = date.fromisoformat(f"{target_month}-01")
        items = [_row_to_item(row) for row in source_rows]
        total_result = total(items, month)
        if total_result.invalid_blocks:
            return _failed()

        organize_config = {
            "mode": "real",
            "source_database_id": profile["source"]["id"],
            "archive_tables_page_id": profile["archive_container"]["id"],
            "field_mapping": {
                "task": "Task", "done": "Done", "category": "Category",
                "takeaway": "Takeaway", "improvement": "Improvement",
            },
            "archive_tables": {
                category: profile["archives"][category]["id"] for category in categories
            },
        }
        preview_count = sum(
            real_eligible_candidate(_row_to_page(row), organize_config) is not None
            for row in source_rows
        )
        totals = {category: str(total_result.totals[category]) for category in sorted(total_result.totals)}
        grand_total = str(sum(total_result.totals.values(), Decimal("0")))
        return NewSystemReadinessReport(
            "ready", totals, grand_total, preview_count, ("total", "organize-preview"),
        )
    except Exception:
        return _failed()
