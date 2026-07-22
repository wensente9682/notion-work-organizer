#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import stat
import sys


SKILL_SOURCE = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TARGET = Path.home() / ".agents" / "skills" / "todo-archive-review"
IGNORED_NAMES = {".DS_Store", ".repository-root", "__pycache__"}
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class InstallError(Exception):
    pass


def _open_directory(parent_fd: int, name: str, *, create: bool) -> int:
    try:
        descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise InstallError
    return descriptor


def _verified_parent(target: Path) -> tuple[int, str]:
    if not target.is_absolute() or target.name in {"", ".", ".."}:
        raise InstallError
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        for component in target.parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise InstallError
            child = _open_directory(descriptor, component, create=True)
            os.close(descriptor)
            descriptor = child
        return descriptor, target.name
    except Exception:
        os.close(descriptor)
        raise


def _existing_target(parent_fd: int, target_name: str) -> bool:
    try:
        target_stat = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(target_stat.st_mode):
        raise InstallError
    target_fd = _open_directory(parent_fd, target_name, create=False)
    try:
        try:
            locator_stat = os.stat(
                ".repository-root", dir_fd=target_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            return True
        if not stat.S_ISREG(locator_stat.st_mode):
            raise InstallError
        return True
    finally:
        os.close(target_fd)


def _copy_file(source: Path, destination_fd: int, name: str, mode: int) -> None:
    source_flags = os.O_RDONLY | os.O_NOFOLLOW
    target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    source_fd = os.open(source, source_flags)
    try:
        target_fd = os.open(name, target_flags, mode, dir_fd=destination_fd)
        try:
            os.fchmod(target_fd, mode)
            while True:
                chunk = os.read(source_fd, 64 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    view = view[written:]
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)


def _copy_skill(source: Path, destination_fd: int) -> None:
    with os.scandir(source) as entries:
        for entry in entries:
            if entry.name in IGNORED_NAMES or entry.name.endswith(".pyc"):
                continue
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode):
                os.mkdir(entry.name, stat.S_IMODE(entry_stat.st_mode), dir_fd=destination_fd)
                child_fd = _open_directory(
                    destination_fd, entry.name, create=False
                )
                try:
                    os.fchmod(child_fd, stat.S_IMODE(entry_stat.st_mode))
                    _copy_skill(Path(entry.path), child_fd)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(entry_stat.st_mode):
                _copy_file(
                    Path(entry.path),
                    destination_fd,
                    entry.name,
                    stat.S_IMODE(entry_stat.st_mode),
                )
            else:
                raise InstallError


def _write_locator(destination_fd: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(
        ".repository-root", flags, 0o600, dir_fd=destination_fd
    )
    try:
        os.fchmod(descriptor, 0o600)
        payload = f"{REPOSITORY_ROOT}\n".encode("utf-8")
        while payload:
            written = os.write(descriptor, payload)
            payload = payload[written:]
    finally:
        os.close(descriptor)


def _remove_tree(parent_fd: int, name: str) -> None:
    try:
        item_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(item_stat.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    item_fd = _open_directory(parent_fd, name, create=False)
    try:
        for child in os.listdir(item_fd):
            _remove_tree(item_fd, child)
    finally:
        os.close(item_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _temporary_directory(parent_fd: int) -> tuple[str, int]:
    for _ in range(32):
        name = f".todo-archive-review-install-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return name, _open_directory(parent_fd, name, create=False)
    raise InstallError


def _install(target: Path) -> None:
    parent_fd, target_name = _verified_parent(target)
    temporary_name = ""
    temporary_fd = -1
    candidate_fd = -1
    moved_previous = False
    published = False
    try:
        had_existing = _existing_target(parent_fd, target_name)
        temporary_name, temporary_fd = _temporary_directory(parent_fd)
        os.mkdir("candidate", 0o700, dir_fd=temporary_fd)
        candidate_fd = _open_directory(temporary_fd, "candidate", create=False)
        _copy_skill(SKILL_SOURCE, candidate_fd)
        _write_locator(candidate_fd)
        os.close(candidate_fd)
        candidate_fd = -1
        if had_existing:
            os.replace(
                target_name,
                "previous",
                src_dir_fd=parent_fd,
                dst_dir_fd=temporary_fd,
            )
            moved_previous = True
        try:
            os.replace(
                "candidate",
                target_name,
                src_dir_fd=temporary_fd,
                dst_dir_fd=parent_fd,
            )
            published = True
        except OSError:
            if moved_previous:
                os.replace(
                    "previous",
                    target_name,
                    src_dir_fd=temporary_fd,
                    dst_dir_fd=parent_fd,
                )
                moved_previous = False
            raise
        if moved_previous:
            _remove_tree(temporary_fd, "previous")
            moved_previous = False
    finally:
        if candidate_fd >= 0:
            os.close(candidate_fd)
        if moved_previous and not published:
            try:
                os.replace(
                    "previous",
                    target_name,
                    src_dir_fd=temporary_fd,
                    dst_dir_fd=parent_fd,
                )
            except OSError:
                pass
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name:
            _remove_tree(parent_fd, temporary_name)
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Organize Codex skill.")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args(argv)
    try:
        _install(args.target.expanduser())
    except (InstallError, OSError):
        print("error: skill installation is unsafe or failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
