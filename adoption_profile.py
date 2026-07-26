"""Approved, local-only persistence for an inspected adoption profile."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import time
from typing import Any, Callable, Mapping

from adopt_inspector import AdoptionReport, _opaque_bindings_digest


PROFILE_KEYS = {
    "mode",
    "source_database_id",
    "archive_tables_page_id",
    "source_order",
    "batch_size",
    "move_limit",
    "field_mapping",
    "archive_tables",
    "project_categories",
    "total",
}
SOURCE_FIELDS = ("task", "done", "category", "takeaway", "improvement")
TOTAL_FIELDS = ("done", "categories", "timeboxing", "date_anchor")
APPROVAL_TTL_SECONDS = 5 * 60


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class FrozenInspectionEvidence:
    state: str
    inspected_at: float
    _content: bytes = field(repr=False)
    _digest: str = field(repr=False)


@dataclass
class ProfileProposal:
    summary: str
    approval_phrase: str
    expires_at: float
    _destination: Path = field(repr=False)
    _content: bytes = field(repr=False)
    _expected_state: str | None = field(repr=False)
    _root: Path = field(repr=False)
    _root_identity: tuple[int, int] = field(repr=False)
    _parent_identity: tuple[int, int] | None = field(repr=False)
    _inspection_digest: str = field(repr=False)
    _ignore_checker: Callable[[Path], bool] = field(repr=False)
    _used: bool = field(default=False, repr=False)


@dataclass(frozen=True)
class ProfileWriteResult:
    status: str
    summary: str


def _fail() -> ProfileError:
    return ProfileError("private adoption profile operation failed safely")


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_mapping(value: Any, required: tuple[str, ...] = ()) -> bool:
    return (
        isinstance(value, Mapping)
        and all(_nonempty_string(key) and _nonempty_string(item) for key, item in value.items())
        and all(_nonempty_string(value.get(key)) for key in required)
    )


def _validated_content(profile: Any) -> bytes:
    if not isinstance(profile, Mapping) or set(profile) - PROFILE_KEYS:
        raise _fail()
    if (
        profile.get("mode") != "real"
        or not _nonempty_string(profile.get("source_database_id"))
        or not _nonempty_string(profile.get("archive_tables_page_id"))
        or not _string_mapping(profile.get("field_mapping"), SOURCE_FIELDS)
        or not _string_mapping(profile.get("archive_tables"))
        or not profile.get("archive_tables")
    ):
        raise _fail()
    total = profile.get("total")
    if (
        not isinstance(total, Mapping)
        or set(total) - {"view_id", "fields", "category_relations"}
        or not _nonempty_string(total.get("view_id"))
        or not _string_mapping(total.get("fields"), TOTAL_FIELDS)
    ):
        raise _fail()
    relations = total.get("category_relations", {})
    if not _string_mapping(relations):
        raise _fail()
    projects = profile.get("project_categories", {})
    if not _string_mapping(projects):
        raise _fail()
    if projects and dict(relations) != {value: key for key, value in projects.items()}:
        raise _fail()
    try:
        return (
            json.dumps(
                dict(profile),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _fail() from None


def _git_ignored(path: Path) -> bool:
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


def _is_ignored(checker: Callable[[Path], bool], path: Path) -> bool:
    try:
        return checker(path) is True
    except Exception:
        return False


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _open_directory(path: Path) -> int:
    try:
        path_stat = path.lstat()
        if not stat.S_ISDIR(path_stat.st_mode):
            raise _fail()
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        if _identity(os.fstat(descriptor)) != _identity(path_stat):
            os.close(descriptor)
            raise _fail()
        return descriptor
    except OSError:
        raise _fail() from None


def _open_child_directory(parent_fd: int, name: str) -> int:
    try:
        item_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(item_stat.st_mode):
            raise _fail()
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        if _identity(os.fstat(descriptor)) != _identity(item_stat):
            os.close(descriptor)
            raise _fail()
        return descriptor
    except OSError:
        raise _fail() from None


def _freeze_destination(
    path: Path,
) -> tuple[Path, tuple[int, int], tuple[int, int] | None, str | None]:
    root = path.parent.parent
    root_fd = _open_directory(root)
    try:
        root_identity = _identity(os.fstat(root_fd))
        try:
            os.stat(path.parent.name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return root, root_identity, None, None
        parent_fd = _open_child_directory(root_fd, path.parent.name)
        try:
            return (
                root,
                root_identity,
                _identity(os.fstat(parent_fd)),
                _state_at(parent_fd, path.name),
            )
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)


def _inspection_state(report: AdoptionReport, accept_partial: bool) -> str:
    statuses = {check.status for check in report.checks}
    if report.status == "ready" and statuses <= {"ready"}:
        return "ready"
    if (
        accept_partial
        and "incompatible" not in statuses
        and statuses <= {"ready", "missing"}
        and "missing" in statuses
    ):
        return "accepted partial"
    raise _fail()


def freeze_inspection_evidence(
    report: AdoptionReport,
    confirmed_profile: Mapping[str, Any],
    *,
    inspected_at: float,
    accept_partial: bool = False,
) -> FrozenInspectionEvidence:
    state = _inspection_state(report, accept_partial)
    confirmed_content = _validated_content(confirmed_profile)
    confirmed_digest = _opaque_bindings_digest(confirmed_profile)
    if (
        not isinstance(report._bindings_digest, str)
        or confirmed_digest != report._bindings_digest
        or not isinstance(inspected_at, (int, float))
    ):
        raise _fail()
    digest = hashlib.sha256(
        confirmed_content
        + report._bindings_digest.encode("ascii")
        + json.dumps(
            [
                report.status,
                [(check.code, check.status) for check in report.checks],
                inspected_at,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return FrozenInspectionEvidence(
        state=state,
        inspected_at=float(inspected_at),
        _content=confirmed_content,
        _digest=digest,
    )


def _approval_phrase(
    content: bytes,
    inspection_digest: str,
    expected_state: str | None,
    expires_at: float,
    destination: Path,
    root_identity: tuple[int, int],
    parent_identity: tuple[int, int] | None,
) -> str:
    digest = hashlib.sha256(
        content
        + inspection_digest.encode("ascii")
        + (expected_state or "missing").encode("ascii")
        + str(expires_at).encode("ascii")
        + str(destination).encode("utf-8")
        + repr(root_identity).encode("ascii")
        + repr(parent_identity).encode("ascii")
    ).hexdigest()[:12]
    return f"approve local profile {digest}"


def propose_adoption_profile(
    evidence: FrozenInspectionEvidence,
    profile: Mapping[str, Any],
    destination: Path,
    *,
    now: Callable[[], float] = time.time,
    ignore_checker: Callable[[Path], bool] = _git_ignored,
) -> ProfileProposal:
    current_time = now()
    content = _validated_content(profile)
    if (
        content != evidence._content
        or evidence.inspected_at > current_time
        or current_time - evidence.inspected_at > APPROVAL_TTL_SECONDS
        or destination.name != "real_profile.json"
        or destination.parent.name != ".todo_archive"
        or not _is_ignored(ignore_checker, destination)
    ):
        raise _fail()
    root, root_identity, parent_identity, expected_state = _freeze_destination(
        destination
    )
    expires_at = current_time + APPROVAL_TTL_SECONDS
    inspection_digest = evidence._digest
    approval_phrase = _approval_phrase(
        content,
        inspection_digest,
        expected_state,
        expires_at,
        destination,
        root_identity,
        parent_identity,
    )
    summary = (
        "Private adoption profile proposal\n"
        f"- Inspection evidence: {evidence.state}\n"
        "- Bindings: source list, ordered view, work fields, Category routes, and Total fields\n"
        "- Destination: ignored private profile\n"
        "- Notion writes: not authorized\n"
        f"- Approval required: {approval_phrase}"
    )
    return ProfileProposal(
        summary=summary,
        approval_phrase=approval_phrase,
        expires_at=expires_at,
        _destination=destination,
        _content=content,
        _expected_state=expected_state,
        _root=root,
        _root_identity=root_identity,
        _parent_identity=parent_identity,
        _inspection_digest=inspection_digest,
        _ignore_checker=ignore_checker,
    )


def _approved_parent(proposal: ProfileProposal) -> int:
    root_fd = _open_directory(proposal._root)
    try:
        if _identity(os.fstat(root_fd)) != proposal._root_identity:
            raise _fail()
        parent_name = proposal._destination.parent.name
        if proposal._parent_identity is None:
            try:
                os.stat(parent_name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(parent_name, 0o700, dir_fd=root_fd)
                except OSError:
                    raise _fail() from None
            else:
                raise _fail()
            parent_fd = _open_child_directory(root_fd, parent_name)
        else:
            parent_fd = _open_child_directory(root_fd, parent_name)
            if _identity(os.fstat(parent_fd)) != proposal._parent_identity:
                os.close(parent_fd)
                raise _fail()
        os.fchmod(parent_fd, 0o700)
        return parent_fd
    finally:
        os.close(root_fd)


def _state_at(parent_fd: int, name: str) -> str | None:
    try:
        target_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(target_stat.st_mode):
        raise _fail()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        raise _fail() from None


def _atomic_write(parent_fd: int, name: str, content: bytes) -> None:
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise _fail() from None


def persist_approved_adoption_profile(
    proposal: ProfileProposal,
    approval: str,
    *,
    now: Callable[[], float] = time.time,
) -> ProfileWriteResult:
    if (
        proposal._used
        or approval != proposal.approval_phrase
        or proposal.approval_phrase
        != _approval_phrase(
            proposal._content,
            proposal._inspection_digest,
            proposal._expected_state,
            proposal.expires_at,
            proposal._destination,
            proposal._root_identity,
            proposal._parent_identity,
        )
        or now() > proposal.expires_at
        or not _is_ignored(proposal._ignore_checker, proposal._destination)
    ):
        raise _fail()
    proposal._used = True
    parent_fd = _approved_parent(proposal)
    try:
        if _state_at(parent_fd, proposal._destination.name) != proposal._expected_state:
            raise _fail()
        _atomic_write(parent_fd, proposal._destination.name, proposal._content)
    finally:
        os.close(parent_fd)
    return ProfileWriteResult(
        "ready",
        "Private adoption profile saved; no Notion write was authorized or performed.",
    )
