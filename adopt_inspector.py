"""Read-only compatibility inspection for adopting an existing Notion system."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping


READY = "ready"
MISSING = "missing"
INCOMPATIBLE = "incompatible"

SOURCE_TYPES = {
    "task": {"title"},
    "done": {"checkbox"},
    "category": {"rich_text", "relation", "select", "multi_select", "status"},
    "takeaway": {"rich_text"},
    "improvement": {"rich_text"},
}
TOTAL_TYPES = {
    "done": {"checkbox"},
    "categories": {"relation", "select", "multi_select", "status"},
    "timeboxing": {"rich_text", "title"},
    "date_anchor": {"date", "rich_text", "title"},
}
ARCHIVE_ROLES = ("task", "takeaway", "improvement")


@dataclass(frozen=True)
class AdoptionRequest:
    source: str
    archive_targets: tuple[str, ...]
    archive_container: str
    ordered_view: str
    source_fields: Mapping[str, str]
    total_fields: Mapping[str, str]
    category_routes: Mapping[str, str]
    category_relations: Mapping[str, str]
    profile: Mapping[str, Any]


@dataclass(frozen=True)
class CompatibilityCheck:
    code: str
    status: str
    next_action: str


@dataclass(frozen=True)
class AdoptionReport:
    status: str
    checks: tuple[CompatibilityCheck, ...]
    next_action: str
    _bindings_digest: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def render(self) -> str:
        lines = [f"Adoption compatibility: {self.status}"]
        lines.extend(
            f"- {check.code}: {check.status}; {check.next_action}"
            for check in self.checks
        )
        lines.append(f"Next action: {self.next_action}")
        return "\n".join(lines)


def _check(code: str, status: str, action: str) -> CompatibilityCheck:
    return CompatibilityCheck(code, status, action)


def _property_type(properties: Any, name: Any) -> str | None:
    if not isinstance(properties, Mapping) or not isinstance(name, str):
        return None
    value = properties.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("type"), str):
        return value["type"]
    return None


def _field_check(
    code: str,
    properties: Any,
    mapping: Mapping[str, str],
    role: str,
    allowed: set[str],
) -> CompatibilityCheck:
    name = mapping.get(role)
    if not isinstance(name, str) or not name or not isinstance(properties, Mapping):
        return _check(code, MISSING, f"map the required {role} field")
    if name not in properties:
        return _check(code, MISSING, f"map or add the required {role} field")
    if _property_type(properties, name) not in allowed:
        return _check(code, INCOMPATIBLE, f"use a supported {role} property type")
    return _check(code, READY, "no action")


def _snapshot_problem(snapshot: Any) -> str | None:
    if not isinstance(snapshot, Mapping):
        return INCOMPATIBLE
    if snapshot.get("ambiguous") is not False:
        return INCOMPATIBLE
    if snapshot.get("complete") is not True:
        return INCOMPATIBLE
    return None


def _opaque_bindings_digest(profile: Any) -> str | None:
    if not isinstance(profile, Mapping):
        return None
    try:
        normalized = json.dumps(
            dict(profile),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(normalized).hexdigest()


def _profile_matches_request(request: AdoptionRequest) -> bool:
    profile = request.profile
    if not isinstance(profile, Mapping):
        return False
    total = profile.get("total")
    if not isinstance(total, Mapping):
        return False
    projects = profile.get("project_categories", {})
    expected_projects = {
        category: relation_id
        for relation_id, category in request.category_relations.items()
    }
    return (
        profile.get("mode") == "real"
        and profile.get("source_database_id") == request.source
        and profile.get("archive_tables_page_id") == request.archive_container
        and profile.get("field_mapping") == request.source_fields
        and profile.get("archive_tables") == request.category_routes
        and set(request.archive_targets) == set(request.category_routes.values())
        and total.get("view_id") == request.ordered_view
        and total.get("fields") == request.total_fields
        and total.get("category_relations", {}) == request.category_relations
        and projects == expected_projects
        and _opaque_bindings_digest(profile) is not None
    )


def _failed_report() -> AdoptionReport:
    codes = (
        "read.source",
        "source.task",
        "source.done",
        "source.category",
        "source.takeaway",
        "source.improvement",
        "category.routing",
        "archive.payload",
        "view.access",
        "view.order",
        "total.done",
        "total.category",
        "total.block",
        "total.date-anchor",
    )
    checks = tuple(
        _check(code, INCOMPATIBLE, "restore unambiguous complete read access and inspect again")
        for code in codes
    )
    return AdoptionReport(
        "not-ready",
        checks,
        "restore unambiguous complete read access and inspect again",
    )


def inspect_existing_system(reader: Any, request: AdoptionRequest) -> AdoptionReport:
    """Inspect structural compatibility without writing or returning private values."""
    if not _profile_matches_request(request):
        return _failed_report()
    bindings_digest = _opaque_bindings_digest(request.profile)
    try:
        source = reader.read_source_schema(request.source)
        if _snapshot_problem(source):
            return _failed_report()

        archives = [
            reader.read_archive_schema(reference)
            for reference in dict.fromkeys(request.archive_targets)
        ]
        if any(
            _snapshot_problem(snapshot)
            or snapshot.get("archive_container") != request.archive_container
            for snapshot in archives
        ):
            return _failed_report()

        view = reader.read_ordered_view(request.ordered_view)
        if _snapshot_problem(view):
            return _failed_report()
    except Exception:
        return _failed_report()

    source_properties = source.get("properties")
    checks = [
        _check("read.source", READY, "no action"),
        *(
            _field_check(
                f"source.{role}",
                source_properties,
                request.source_fields,
                role,
                allowed,
            )
            for role, allowed in SOURCE_TYPES.items()
        ),
    ]

    values = source.get("category_values")
    routes = request.category_routes
    route_targets = set(routes.values()) if isinstance(routes, Mapping) else set()
    declared_targets = set(request.archive_targets)
    if source.get("category_values_complete") is not True or not isinstance(values, list):
        routing = _check(
            "category.routing",
            INCOMPATIBLE,
            "complete the bounded Category read and inspect again",
        )
    elif any(not isinstance(value, str) or not value for value in values):
        routing = _check(
            "category.routing",
            INCOMPATIBLE,
            "resolve empty or malformed Category values",
        )
    elif any(value not in routes for value in values) or not route_targets <= declared_targets:
        routing = _check(
            "category.routing",
            MISSING,
            "map every observed Category to an inspected archive target",
        )
    else:
        routing = _check("category.routing", READY, "no action")
    checks.append(routing)

    if not archives:
        archive_check = _check(
            "archive.payload",
            MISSING,
            "select and inspect at least one archive target",
        )
    else:
        archive_status = READY
        for snapshot in archives:
            properties = snapshot.get("properties")
            for role in ARCHIVE_ROLES:
                field = _field_check(
                    "archive.payload",
                    properties,
                    request.source_fields,
                    role,
                    SOURCE_TYPES[role],
                )
                if field.status == INCOMPATIBLE:
                    archive_status = INCOMPATIBLE
                elif field.status == MISSING and archive_status == READY:
                    archive_status = MISSING
        archive_action = {
            READY: "no action",
            MISSING: "map or add the portable archive payload fields",
            INCOMPATIBLE: "use supported portable archive payload property types",
        }[archive_status]
        archive_check = _check("archive.payload", archive_status, archive_action)
    checks.append(archive_check)

    if view.get("accessible") is not True:
        checks.append(_check("view.access", INCOMPATIBLE, "restore read access to the configured view"))
    else:
        checks.append(_check("view.access", READY, "no action"))
    if view.get("saved_order_verified") is not True:
        checks.append(_check("view.order", INCOMPATIBLE, "provide a verifiably ordered configured view"))
    else:
        checks.append(_check("view.order", READY, "no action"))

    for role, allowed in TOTAL_TYPES.items():
        code_role = {
            "categories": "category",
            "timeboxing": "block",
            "date_anchor": "date-anchor",
        }.get(role, role)
        checks.append(
            _field_check(
                f"total.{code_role}",
                source_properties,
                request.total_fields,
                role,
                allowed,
            )
        )

    if view.get("date_anchors_verified") is not True:
        index = next(
            index for index, check in enumerate(checks) if check.code == "total.date-anchor"
        )
        checks[index] = _check(
            "total.date-anchor",
            INCOMPATIBLE,
            "verify structured date anchors in saved view order",
        )

    all_ready = all(check.status == READY for check in checks)
    report = AdoptionReport(
        READY if all_ready else "not-ready",
        tuple(checks),
        (
            "request separate approval to generate or update the ignored private profile"
            if all_ready
            else "resolve reported missing or incompatible requirements, then inspect again"
        ),
    )
    object.__setattr__(report, "_bindings_digest", bindings_digest)
    return report
