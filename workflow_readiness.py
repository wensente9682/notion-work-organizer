"""Read-only Total then Organize readiness from one approved private profile."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import time
from typing import Any, Callable, Mapping

from adoption_profile import (
    APPROVAL_TTL_SECONDS,
    FrozenInspectionEvidence,
    _validated_content,
)
from notion_todo_workflow import (
    WorkflowError,
    archive_schema_expectations,
    archive_target_index_from_content,
    real_eligible_candidate,
    require_archive_table_target_from_index,
    require_props,
    require_real_source_props,
)
from total_views import TotalViewFields, total_from_view


class WorkflowReadinessError(Exception):
    pass


@dataclass(frozen=True)
class WorkflowReadinessReport:
    status: str
    category_totals: Mapping[str, str]
    grand_total: str | None
    preview_count: int | None
    summary: str


@dataclass(frozen=True)
class _ProfileSnapshot:
    profile: dict[str, Any]
    identity: tuple[int, int, int, int]
    content_digest: str


def _ignored(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def _fail() -> WorkflowReadinessError:
    return WorkflowReadinessError("one-profile readiness could not be verified safely")


def _read_profile(
    path: Path,
    evidence: FrozenInspectionEvidence,
    ignore_checker: Callable[[Path], bool],
) -> _ProfileSnapshot:
    try:
        parent_stat = path.parent.lstat()
        path_stat = path.lstat()
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or stat.S_ISLNK(parent_stat.st_mode)
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
            or not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or stat.S_IMODE(path_stat.st_mode) != 0o600
            or ignore_checker(path) is not True
        ):
            raise _fail()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            content = handle.read()
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            raise _fail()
        profile = json.loads(content.decode("utf-8"))
        if (
            not isinstance(evidence, FrozenInspectionEvidence)
            or _validated_content(profile) != evidence._content
        ):
            raise _fail()
        return _ProfileSnapshot(
            profile=dict(profile),
            identity=(
                path_stat.st_dev,
                path_stat.st_ino,
                path_stat.st_size,
                path_stat.st_mtime_ns,
            ),
            content_digest=hashlib.sha256(content).hexdigest(),
        )
    except WorkflowReadinessError:
        raise
    except Exception:
        raise _fail() from None


def _same_profile(
    path: Path,
    expected: _ProfileSnapshot,
    evidence: FrozenInspectionEvidence,
    ignore_checker: Callable[[Path], bool],
) -> None:
    current = _read_profile(path, evidence, ignore_checker)
    if (
        current.identity != expected.identity
        or current.content_digest != expected.content_digest
    ):
        raise _fail()


def _total_fields(profile: Mapping[str, Any]) -> tuple[str, TotalViewFields]:
    total_config = profile["total"]
    fields = total_config["fields"]
    source_fields = profile["field_mapping"]
    if (
        fields["done"] != source_fields["done"]
        or fields["categories"] != source_fields["category"]
    ):
        raise _fail()
    return total_config["view_id"], TotalViewFields(
        done=fields["done"],
        categories=fields["categories"],
        timeboxing=fields["timeboxing"],
        date_anchor=fields["date_anchor"],
        category_relations=total_config.get("category_relations", {}),
    )


def _organize_preview_count(reader: Any, profile: dict[str, Any]) -> int:
    try:
        require_real_source_props(
            reader.retrieve_database(profile["source_database_id"]),
            profile,
        )
        content = reader.retrieve_archive_tables_content(
            profile["archive_tables_page_id"]
        )
        if not isinstance(content, str):
            raise WorkflowError("archive target read is incomplete")
        target_index = archive_target_index_from_content(content)
        for category in profile["archive_tables"]:
            target = require_archive_table_target_from_index(
                profile,
                target_index,
                category,
            )
            require_props(
                reader.retrieve_database(target.database_id),
                archive_schema_expectations(profile),
                "archive",
            )

        count = 0
        cursor = None
        seen_cursors: set[str] = set()
        for _ in range(10):
            response = reader.query_database(
                profile["source_database_id"],
                start_cursor=cursor,
                page_size=100,
            )
            if (
                not isinstance(response, dict)
                or not isinstance(response.get("results"), list)
                or type(response.get("has_more")) is not bool
            ):
                raise WorkflowError("source preview read is incomplete")
            for page in response["results"]:
                if not isinstance(page, dict):
                    raise WorkflowError("source preview row is malformed")
                if real_eligible_candidate(page, profile) is not None:
                    count += 1
                    if count >= int(profile.get("batch_size", 5)):
                        return count
            if response["has_more"] is False:
                if response.get("next_cursor") is not None:
                    raise WorkflowError("source preview terminal cursor is invalid")
                return count
            next_cursor = response.get("next_cursor")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor.strip()
                or next_cursor in seen_cursors
            ):
                raise WorkflowError("source preview cursor is invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise WorkflowError("source preview is incomplete")
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError("source preview failed") from exc


def verify_one_profile_readiness(
    profile_path: Path,
    evidence: FrozenInspectionEvidence,
    target_month: str,
    *,
    total_api: Any,
    organize_reader: Any,
    ignore_checker: Callable[[Path], bool] = _ignored,
    now: Callable[[], float] = time.time,
) -> WorkflowReadinessReport:
    """Verify Total first, then a read-only Organize preview, without approvals."""
    try:
        if (
            not isinstance(target_month, str)
            or len(target_month) != 7
            or target_month[4] != "-"
        ):
            raise _fail()
        month = date.fromisoformat(f"{target_month}-01")
        current_time = now()
        inspected_at = evidence.inspected_at
        if (
            type(current_time) not in {int, float}
            or type(inspected_at) not in {int, float}
            or not math.isfinite(current_time)
            or not math.isfinite(inspected_at)
            or inspected_at > current_time
            or current_time - inspected_at > APPROVAL_TTL_SECONDS
        ):
            raise _fail()
        snapshot = _read_profile(profile_path, evidence, ignore_checker)
        view_id, fields = _total_fields(snapshot.profile)
        total_result = total_from_view(
            total_api,
            view_id=view_id,
            target_month=month,
            fields=fields,
        )
        if total_result.invalid_blocks:
            raise _fail()
        _same_profile(profile_path, snapshot, evidence, ignore_checker)
        preview_count = _organize_preview_count(organize_reader, snapshot.profile)
        _same_profile(profile_path, snapshot, evidence, ignore_checker)
        category_totals = {
            category: str(total_result.totals[category])
            for category in sorted(total_result.totals)
        }
        grand_total = str(sum(total_result.totals.values(), Decimal("0")))
        return WorkflowReadinessReport(
            status="ready",
            category_totals=category_totals,
            grand_total=grand_total,
            preview_count=preview_count,
            summary=(
                "One-profile readiness: ready. Total was verified first; "
                "Organize reached read-only preview. No approval was granted."
            ),
        )
    except Exception:
        return WorkflowReadinessReport(
            status="not-ready",
            category_totals={},
            grand_total=None,
            preview_count=None,
            summary=(
                "One-profile readiness: not-ready. Re-run the read-only "
                "compatibility inspection before continuing."
            ),
        )
