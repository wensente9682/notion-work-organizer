"""Fail-closed local finalization for a verified v0.3 New System."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import Callable

from new_system_classifier import classify_new_system
from new_system_blueprint import ARCHIVE_PROPERTIES, SOURCE_PROPERTIES, validate_canonical_blueprint


_PROFILE_NAME = "new_system_profile.json"
_MAX_DEPTH = 12
_MAX_NODES = 256
_MAX_CONTAINER = 64
_RENAME_EXCL = 0x00000004
_RENAMEATX = getattr(ctypes.CDLL(None, use_errno=True), "renameatx_np", None)
if _RENAMEATX is not None:
    _RENAMEATX.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    _RENAMEATX.restype = ctypes.c_int


@dataclass(frozen=True)
class FinalizationReport:
    status: str
    phase: str
    operation_id: str
    structure_count: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "phase": self.phase,
            "operation_id": self.operation_id,
            "structure_count": self.structure_count,
        }


@dataclass(frozen=True)
class ProfileGenerationResult:
    """A validated, unpublished private-profile payload."""

    status: str
    content: bytes | None


@dataclass(frozen=True)
class _ExpectedProfile:
    root: tuple[int, ...]
    parent: tuple[int, ...]
    file: tuple[int, ...]
    digest: str


class NewSystemFinalizer:
    """Own one no-replace profile and the Total -> Organize read-only seam."""

    def __init__(
        self,
        profile_path: Path,
        *,
        ignore_checker: Callable[[Path], bool],
    ) -> None:
        self._path = profile_path
        self._ignore_checker = ignore_checker
        self._running = threading.Lock()

    @staticmethod
    def _fail_report(phase: str, operation_id: str, reads: int) -> FinalizationReport:
        return FinalizationReport("not-ready", phase, operation_id, 4)

    def generate_profile(self, snapshot: object) -> ProfileGenerationResult:
        """Generate a canonical profile without publishing it or running product features."""
        materialized = _materialize(snapshot)
        if (
            materialized is None
            or validate_canonical_blueprint(materialized).status != "ready"
        ):
            return ProfileGenerationResult("not-ready", None)
        content = self._profile_content(materialized)
        if content is None:
            return ProfileGenerationResult("not-ready", None)
        try:
            profile = json.loads(content)
        except (TypeError, ValueError, UnicodeError):
            return ProfileGenerationResult("not-ready", None)
        if type(profile) is not dict or profile.get("mode") != "new-system-v0.3":
            return ProfileGenerationResult("not-ready", None)
        return ProfileGenerationResult("ready", content)

    def finalize(self, snapshot_reader: object, total_api: object, organize_reader: object) -> FinalizationReport:
        operation_id = secrets.token_hex(16)
        if not self._running.acquire(blocking=False):
            return self._fail_report("claim", operation_id, 0)
        phase, reads = "claim", 0
        try:
            snapshot = self._read_snapshot(snapshot_reader)
            reads += 1
            if (
                snapshot is None
                or classify_new_system(snapshot).status != "ready-to-preview"
                or validate_canonical_blueprint(snapshot).status != "ready"
            ):
                return self._fail_report("exact-reinspected", operation_id, reads)
            phase = "ready-snapshot"
            content = self._profile_content(snapshot)
            if content is None:
                return self._fail_report("ready-snapshot", operation_id, reads)
            phase = "publishing-profile"
            expected = self._publish(content)
            if expected is None:
                return self._fail_report(phase, operation_id, reads)
            phase = "profile-published"
            profile = self._reverify(expected, content)
            if profile is None:
                return self._fail_report("reverifying-profile", operation_id, reads)
            phase = "profile-reverified"
            phase = "verifying-total"
            if not self._call(total_api, "verify_read_only", profile):
                return self._fail_report(phase, operation_id, reads)
            phase = "total-verified"
            profile = self._reverify(expected, content)
            if profile is None:
                return self._fail_report("reverifying-profile", operation_id, reads)
            phase = "previewing-organize"
            if not self._call(organize_reader, "preview_read_only", profile):
                return self._fail_report(phase, operation_id, reads)
            phase = "organize-previewed"
            if self._reverify(expected, content) is None:
                return self._fail_report("reverifying-profile", operation_id, reads)
            return FinalizationReport("ready", "ready", operation_id, 4)
        except Exception:
            return self._fail_report(phase, operation_id, reads)
        finally:
            self._running.release()

    def _read_snapshot(self, reader: object) -> dict[str, object] | None:
        try:
            value = reader.read_exact()
            return _materialize(value)
        except Exception:
            return None

    def _profile_content(self, snapshot: dict[str, object]) -> bytes | None:
        try:
            source = snapshot["source"]
            view = snapshot["view"]
            container = snapshot["archive_container"]
            archives = snapshot["archives"]
            if not all(type(item) is dict for item in (source, view, container, archives)):
                return None
            if not all(type(item.get("id")) is str and item["id"].strip() for item in (source, view, container)):
                return None
            safe_archives = {}
            for category, archive in archives.items():
                if type(category) is not str or type(archive) is not dict:
                    return None
                safe_archives[category] = {
                    "id": archive["id"],
                    "parent_id": archive["parent_id"],
                    "category": archive["category"],
                    "properties": {name: archive["properties"][name] for name in ARCHIVE_PROPERTIES},
                }
            profile = {
                "mode": "new-system-v0.3",
                "source": {
                    "id": source["id"],
                    "properties": {name: source["properties"][name] for name in SOURCE_PROPERTIES},
                    "categories": source["category_options"],
                },
                "view": {"id": view["id"], "sort": {"property": view["sort"]["property"], "direction": view["sort"]["direction"]}},
                "archive_container": {"id": container["id"]},
                "archives": safe_archives,
            }
            return (json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n").encode()
        except Exception:
            return None

    def _safe_parent(self) -> tuple[int, tuple[int, ...], tuple[int, ...]] | None:
        if self._path.name != _PROFILE_NAME or self._path.parent.name != ".todo_archive":
            return None
        root = self._path.parent.parent
        try:
            root_stat = root.lstat()
            if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
                return None
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened_root = os.fstat(root_fd)
                if _directory_identity(opened_root) != _directory_identity(root_stat):
                    return None
                try:
                    item = os.stat(".todo_archive", dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.mkdir(".todo_archive", 0o700, dir_fd=root_fd)
                    item = os.stat(".todo_archive", dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISDIR(item.st_mode) or stat.S_ISLNK(item.st_mode) or item.st_uid != os.getuid():
                    return None
                parent_fd = -1
                try:
                    parent_fd = os.open(".todo_archive", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
                    opened = os.fstat(parent_fd)
                    if _directory_identity(opened) != _directory_identity(item) or not _safe_directory(opened):
                        return None
                    # mkdir may have changed the root directory ctime; capture the
                    # authority baseline only after that local setup is complete.
                    current_root = os.fstat(root_fd)
                    if not stat.S_ISDIR(current_root.st_mode) or stat.S_ISLNK(current_root.st_mode):
                        return None
                    result = (parent_fd, _directory_identity(current_root), _directory_identity(opened))
                    parent_fd = -1
                    return result
                finally:
                    if parent_fd >= 0:
                        os.close(parent_fd)
            finally:
                os.close(root_fd)
        except Exception:
            return None

    def _publish(self, content: bytes) -> _ExpectedProfile | None:
        if not self._ignored():
            return None
        parent = self._safe_parent()
        if parent is None:
            return None
        parent_fd, root_identity, parent_identity = parent
        descriptor = -1
        final_fd = -1
        try:
            temporary = ".new-system-" + secrets.token_hex(16) + ".tmp"
            temporary_path = self._path.parent / temporary
            if not self._ignored_path(self._path.parent) or not self._ignored_path(temporary_path):
                return None
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
            view = memoryview(content)
            writes = 0
            while view:
                written = os.write(descriptor, view)
                writes += 1
                if type(written) is not int or written <= 0 or writes > 16:
                    return None
                view = view[written:]
            os.fsync(descriptor)
            saved = os.fstat(descriptor)
            if not _safe_file(saved, 0o600, 1):
                return None
            item = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
            if not _safe_file(item, 0o600, 1) or _file_identity(item) != _file_identity(saved):
                return None
            if not _rename_no_replace(parent_fd, temporary):
                return None
            os.fsync(parent_fd)
            item = os.stat(_PROFILE_NAME, dir_fd=parent_fd, follow_symlinks=False)
            if not _safe_file(item, 0o600, 1) or (item.st_dev, item.st_ino) != (saved.st_dev, saved.st_ino):
                return None
            final_fd = os.open(_PROFILE_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            final = os.fstat(final_fd)
            if not _safe_file(final, 0o600, 1) or (final.st_dev, final.st_ino) != (saved.st_dev, saved.st_ino) or _file_identity(final) != _file_identity(item):
                return None
            settled = self._safe_parent()
            if settled is None:
                return None
            settled_fd, settled_root, settled_parent = settled
            os.close(settled_fd)
            # Parent ctime legitimately changes while the temp is published, but
            # it may only be rebased after continuity of its authority fields and
            # complete root identity have both been proven.
            if settled_root != root_identity or settled_parent[:4] != parent_identity[:4]:
                return None
            return _ExpectedProfile(settled_root, settled_parent, _file_identity(final), hashlib.sha256(content).hexdigest())
        except Exception:
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if final_fd >= 0:
                os.close(final_fd)
            os.close(parent_fd)

    def _reverify(self, expected: _ExpectedProfile, content: bytes) -> dict[str, object] | None:
        if not self._ignored():
            return None
        parent = self._safe_parent()
        if parent is None:
            return None
        parent_fd, root_identity, parent_identity = parent
        try:
            if root_identity != expected.root or parent_identity != expected.parent:
                return None
            item = os.stat(_PROFILE_NAME, dir_fd=parent_fd, follow_symlinks=False)
            if not _safe_file(item, 0o600, 1) or _file_identity(item) != expected.file:
                return None
            fd = os.open(_PROFILE_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                opened = os.fstat(fd)
                data = os.read(fd, item.st_size + 1)
                after_fd = os.fstat(fd)
            finally:
                os.close(fd)
            after_path = os.stat(_PROFILE_NAME, dir_fd=parent_fd, follow_symlinks=False)
            if (not _safe_file(opened, 0o600, 1) or not _safe_file(after_fd, 0o600, 1) or not _safe_file(after_path, 0o600, 1) or _file_identity(opened) != expected.file or _file_identity(after_fd) != expected.file or _file_identity(after_path) != expected.file or data != content or hashlib.sha256(data).hexdigest() != expected.digest):
                return None
            ending = self._safe_parent()
            if ending is None:
                return None
            ending_fd, ending_root, ending_parent = ending
            os.close(ending_fd)
            if ending_root != expected.root or ending_parent != expected.parent:
                return None
            decoded = json.loads(data.decode("utf-8"))
            return decoded if type(decoded) is dict else None
        except Exception:
            return None
        finally:
            os.close(parent_fd)

    def _ignored(self) -> bool:
        return self._ignored_path(self._path)

    def _ignored_path(self, path: Path) -> bool:
        try:
            return self._ignore_checker(path) is True
        except Exception:
            return False

    @staticmethod
    def _call(api: object, name: str, profile: dict[str, object]) -> bool:
        try:
            method = getattr(api, name)
            result = method(profile)
            return type(result) is str and result == "ready"
        except Exception:
            return False


def _safe_file(item: os.stat_result, mode: int, links: int) -> bool:
    return stat.S_ISREG(item.st_mode) and not stat.S_ISLNK(item.st_mode) and item.st_uid == os.getuid() and stat.S_IMODE(item.st_mode) == mode and item.st_nlink == links


def _safe_directory(item: os.stat_result) -> bool:
    return stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode) and item.st_uid == os.getuid() and stat.S_IMODE(item.st_mode) == 0o700


def _file_identity(item: os.stat_result) -> tuple[int, ...]:
    return (item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode), item.st_uid, item.st_nlink, item.st_size, item.st_ctime_ns, item.st_mtime_ns)


def _directory_identity(item: os.stat_result) -> tuple[int, ...]:
    return (item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode), item.st_uid, item.st_ctime_ns)


def _rename_no_replace(parent_fd: int, temporary: str) -> bool:
    """Use the platform's atomic no-replace rename; no name-based unlink."""
    if _RENAMEATX is None:
        return False
    try:
        result = _RENAMEATX(
            parent_fd,
            temporary.encode("ascii"),
            parent_fd,
            _PROFILE_NAME.encode("ascii"),
            _RENAME_EXCL,
        )
        return result == 0
    except Exception:
        return False


def _materialize(value: object) -> dict[str, object] | None:
    """Copy only ordinary JSON-like values; never iterate arbitrary protocols."""
    if type(value) is dict:
        budget = [0, 0]
        return _materialize_mapping(value, 0, budget)
    return None


def _materialize_mapping(value: dict[object, object], depth: int, budget: list[int]) -> dict[str, object] | None:
    if depth > _MAX_DEPTH or len(value) > _MAX_CONTAINER:
        return None
    budget[1] += 1
    if budget[1] > _MAX_NODES:
        return None
    try:
        copied: dict[str, object] = {}
        for key, child in value.items():
            if type(key) is not str:
                return None
            material = _materialize_value(child, depth + 1, budget)
            if material is _INVALID:
                return None
            copied[key] = material
        return copied
    except Exception:
        return None


_INVALID = object()


def _materialize_value(value: object, depth: int, budget: list[int]) -> object:
    if depth > _MAX_DEPTH:
        return _INVALID
    budget[0] += 1
    if budget[0] > _MAX_NODES:
        return _INVALID
    if type(value) in {str, int, float, bool} or value is None:
        return value
    if type(value) is dict:
        return _materialize_mapping(value, depth, budget) or _INVALID
    if type(value) in {list, tuple}:
        if len(value) > _MAX_CONTAINER:
            return _INVALID
        copied = []
        for child in value:
            material = _materialize_value(child, depth + 1, budget)
            if material is _INVALID:
                return _INVALID
            copied.append(material)
        return copied
    return _INVALID
