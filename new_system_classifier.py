"""Read-only classification of an already-discovered New System snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from new_system_blueprint import validate_canonical_blueprint


@dataclass(frozen=True)
class ClassificationEvidence:
    kind: str
    code: str
    path: str
    remediation: str


@dataclass(frozen=True)
class ClassificationResult:
    status: str
    evidence: tuple[ClassificationEvidence, ...]


def classify_new_system(snapshot: Mapping[str, object]) -> ClassificationResult:
    if not isinstance(snapshot, Mapping):
        return ClassificationResult(
            "incompatible",
            (
                ClassificationEvidence(
                    "incompatible",
                    "snapshot.shape",
                    "snapshot",
                    "provide one snapshot mapping",
                ),
            ),
        )

    discovery_evidence = []
    allowed_keys = {
        "source",
        "view",
        "archive_container",
        "archives",
        "unrelated_resources",
        "ambiguous_candidates",
    }
    if any(key not in allowed_keys for key in snapshot):
        discovery_evidence.append(
            ClassificationEvidence(
                "incompatible",
                "snapshot.undeclared",
                "snapshot[*]",
                "remove undeclared snapshot resource",
            )
        )

    for key in ("source", "view", "archive_container", "archives"):
        if key in snapshot and not isinstance(snapshot[key], Mapping):
            discovery_evidence.append(
                ClassificationEvidence(
                    "incompatible",
                    "snapshot.resource-shape",
                    f"snapshot.{key}",
                    "provide one mapping for canonical resource",
                )
            )

    nested_shape_paths = []
    source = snapshot.get("source")
    if (
        isinstance(source, Mapping)
        and "properties" in source
        and not isinstance(source["properties"], Mapping)
    ):
        nested_shape_paths.append("snapshot.source.properties")
    view = snapshot.get("view")
    if (
        isinstance(view, Mapping)
        and "sort" in view
        and not isinstance(view["sort"], Mapping)
    ):
        nested_shape_paths.append("snapshot.view.sort")
    archives = snapshot.get("archives")
    if isinstance(archives, Mapping):
        for archive in archives.values():
            if not isinstance(archive, Mapping):
                nested_shape_paths.append("snapshot.archives[*]")
            elif (
                "properties" in archive
                and not isinstance(archive["properties"], Mapping)
            ):
                nested_shape_paths.append("snapshot.archives[*].properties")
    for path in nested_shape_paths:
        discovery_evidence.append(
            ClassificationEvidence(
                "incompatible",
                "snapshot.nested-shape",
                path,
                "provide one mapping for nested canonical resource",
            )
        )

    for key, code in (
        ("unrelated_resources", "discovery.unrelated"),
        ("ambiguous_candidates", "discovery.ambiguous"),
    ):
        if key not in snapshot:
            continue
        candidates = snapshot[key]
        if not isinstance(candidates, (list, tuple)):
            discovery_evidence.append(
                ClassificationEvidence(
                    "incompatible",
                    "discovery.metadata-shape",
                    f"discovery.{key}",
                    "provide a candidate sequence",
                )
            )
        elif candidates:
            discovery_evidence.append(
                ClassificationEvidence(
                    "incompatible",
                    code,
                    f"discovery.{key}",
                    "select only unambiguous canonical resources",
                )
            )
    if discovery_evidence:
        return ClassificationResult("incompatible", tuple(discovery_evidence))
    if any(
        key in snapshot
        for key in ("source", "view", "archive_container", "archives")
    ):
        report = validate_canonical_blueprint(snapshot)
        if report.status == "ready":
            return ClassificationResult("ready-to-preview", ())
        evidence = []
        for issue in report.issues:
            kind = (
                "missing"
                if issue.actual is None or issue.code == "archive.missing"
                else "incompatible"
            )
            path = {
                "archive.missing": "archives[*]",
                "archive.parent": "archives[*].parent_id",
                "archive.id": "archives[*].id",
                "archive.id-unique": "archives[*].id",
                "archive.category": "archives[*].category",
                "archive.schema": "archives[*].properties",
                "archive.unexpected": "archives[*]",
                "source.category": "source.category_options[*]",
                "source.category-unique": "source.category_options[*]",
            }.get(issue.code, issue.path)
            remediation = {
                ("missing", "source.property-type"): "add required source property",
                ("missing", "archive.missing"): "add required category archive",
                ("incompatible", "view.sort"): "restore canonical view sort",
            }.get((kind, issue.code), f"review {kind} canonical binding")
            evidence.append(
                ClassificationEvidence(
                    kind,
                    issue.code,
                    path,
                    remediation,
                )
            )
        if any(item.kind == "incompatible" for item in evidence):
            return ClassificationResult(
                "incompatible",
                tuple(evidence),
            )
        if evidence:
            return ClassificationResult(
                "partially present",
                tuple(evidence),
            )
    return ClassificationResult("blank", ())
