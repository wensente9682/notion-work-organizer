"""Pure-local canonical blueprint for a blank v0.3 New System."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


SOURCE_PROPERTIES = {
    "Task": "title",
    "Done": "checkbox",
    "Category": "select",
    "Takeaway": "rich_text",
    "Improvement": "rich_text",
    "Work Date": "date",
    "Time Blocks": "rich_text",
}
ARCHIVE_PROPERTIES = {
    "Task": "title",
    "Takeaway": "rich_text",
    "Improvement": "rich_text",
}


@dataclass(frozen=True)
class BlueprintIssue:
    code: str
    path: str
    expected: object
    actual: object


@dataclass(frozen=True)
class BlueprintReadiness:
    status: str
    issues: tuple[BlueprintIssue, ...]


def canonical_blueprint(
    *,
    archive_container_id: str,
    archive_databases: Mapping[str, str],
) -> dict[str, object]:
    return {
        "source": {
            "properties": dict(SOURCE_PROPERTIES),
            "category_options": tuple(archive_databases),
        },
        "view": {
            "configured": True,
            "sort": {"property": "Work Date", "direction": "descending"},
        },
        "archive_container": {
            "id": archive_container_id,
            "approved": True,
        },
        "archives": {
            category: {
                "id": database_id,
                "parent_id": archive_container_id,
                "category": category,
                "properties": dict(ARCHIVE_PROPERTIES),
            }
            for category, database_id in archive_databases.items()
        },
    }


def validate_canonical_blueprint(
    blueprint: Mapping[str, object],
) -> BlueprintReadiness:
    source = blueprint.get("source", {})
    properties = source.get("properties", {}) if isinstance(source, Mapping) else {}
    issues = []
    for name, expected in SOURCE_PROPERTIES.items():
        actual = properties.get(name) if isinstance(properties, Mapping) else None
        if actual != expected:
            issues.append(
                BlueprintIssue(
                    "source.property-type",
                    f"source.properties.{name}",
                    expected,
                    actual,
                )
            )
    view = blueprint.get("view", {})
    sort = view.get("sort", {}) if isinstance(view, Mapping) else {}
    configured = view.get("configured") if isinstance(view, Mapping) else None
    if configured is not True:
        issues.append(
            BlueprintIssue(
                "view.configured",
                "view.configured",
                True,
                configured,
            )
        )
    for key, expected in (("property", "Work Date"), ("direction", "descending")):
        actual = sort.get(key) if isinstance(sort, Mapping) else None
        if actual != expected:
            issues.append(
                BlueprintIssue(
                    "view.sort",
                    f"view.sort.{key}",
                    expected,
                    actual,
                )
            )
    container = blueprint.get("archive_container", {})
    container_id = container.get("id") if isinstance(container, Mapping) else None
    container_id_valid = (
        isinstance(container_id, str) and bool(container_id.strip())
    )
    if not container_id_valid:
        issues.append(
            BlueprintIssue(
                "archive-container.id",
                "archive_container.id",
                "non-empty string",
                container_id,
            )
        )
    container_approved = (
        container.get("approved") if isinstance(container, Mapping) else None
    )
    if container_approved is not True:
        issues.append(
            BlueprintIssue(
                "archive-container.approved",
                "archive_container.approved",
                True,
                container_approved,
            )
        )
    archives = blueprint.get("archives", {})
    categories = source.get("category_options") if isinstance(source, Mapping) else None
    categories_valid = isinstance(categories, (list, tuple)) and bool(categories)
    if not categories_valid:
        issues.append(
            BlueprintIssue(
                "source.category-options",
                "source.category_options",
                "non-empty sequence",
                categories,
            )
        )
    declared_categories = set()
    for index, category in enumerate(categories if categories_valid else ()):
        if not isinstance(category, str) or not category.strip():
            issues.append(
                BlueprintIssue(
                    "source.category",
                    f"source.category_options[{index}]",
                    "non-empty string",
                    category,
                )
            )
            continue
        if category in declared_categories:
            issues.append(
                BlueprintIssue(
                    "source.category-unique",
                    f"source.category_options[{index}]",
                    "unique category",
                    category,
                )
            )
        else:
            declared_categories.add(category)
        if not isinstance(archives, Mapping) or category not in archives:
            issues.append(
                BlueprintIssue(
                    "archive.missing",
                    f"archives.{category}",
                    "direct-child archive database",
                    None,
                )
            )
    if isinstance(archives, Mapping):
        seen_archive_ids = set()
        for category, archive in archives.items():
            if categories_valid and category not in declared_categories:
                issues.append(
                    BlueprintIssue(
                        "archive.unexpected",
                        f"archives.{category}",
                        "declared category",
                        category,
                    )
                )
            parent_id = archive.get("parent_id") if isinstance(archive, Mapping) else None
            if not container_id_valid or parent_id != container_id:
                issues.append(
                    BlueprintIssue(
                        "archive.parent",
                        f"archives.{category}.parent_id",
                        (
                            container_id
                            if container_id_valid
                            else "non-empty archive container ID"
                        ),
                        parent_id,
                    )
                )
            archive_id = archive.get("id") if isinstance(archive, Mapping) else None
            if not isinstance(archive_id, str) or not archive_id.strip():
                issues.append(
                    BlueprintIssue(
                        "archive.id",
                        f"archives.{category}.id",
                        "non-empty string",
                        archive_id,
                    )
                )
            elif archive_id in seen_archive_ids:
                issues.append(
                    BlueprintIssue(
                        "archive.id-unique",
                        f"archives.{category}.id",
                        "unique archive database ID",
                        archive_id,
                    )
                )
            else:
                seen_archive_ids.add(archive_id)
            bound_category = archive.get("category") if isinstance(archive, Mapping) else None
            if bound_category != category:
                issues.append(
                    BlueprintIssue(
                        "archive.category",
                        f"archives.{category}.category",
                        category,
                        bound_category,
                    )
                )
            archive_properties = (
                archive.get("properties") if isinstance(archive, Mapping) else {}
            )
            for name, expected in ARCHIVE_PROPERTIES.items():
                actual = (
                    archive_properties.get(name)
                    if isinstance(archive_properties, Mapping)
                    else None
                )
                if actual != expected:
                    issues.append(
                        BlueprintIssue(
                            "archive.schema",
                            f"archives.{category}.properties.{name}",
                            expected,
                            actual,
                        )
                    )
    return BlueprintReadiness("not-ready" if issues else "ready", tuple(issues))
