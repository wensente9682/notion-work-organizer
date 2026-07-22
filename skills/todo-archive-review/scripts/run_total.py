#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import stat
import sys


SCRIPT_PATH = Path(__file__).absolute()
SKILL_DIR = SCRIPT_PATH.parents[1]
EXPECTED_SCRIPT_PARTS = (
    "skills",
    "todo-archive-review",
    "scripts",
    "run_total.py",
)


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _valid_repository_root(path: Path, installed_script: Path) -> bool:
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            return False
        git_stat = os.lstat(path / ".git")
        if not (stat.S_ISDIR(git_stat.st_mode) or stat.S_ISREG(git_stat.st_mode)):
            return False
        expected_script = path.joinpath(*EXPECTED_SCRIPT_PARTS)
        required = (
            path / "total.py",
            path / "total_views.py",
            path / "total_command.py",
            expected_script,
            installed_script,
        )
        if not all(_regular_file(candidate) for candidate in required):
            return False
        return expected_script.read_bytes() == installed_script.read_bytes()
    except OSError:
        return False


def _locator_root(locator: Path, installed_script: Path) -> Path | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if not stat.S_ISREG(os.lstat(locator).st_mode):
            return None
        descriptor = os.open(locator, flags)
        try:
            locator_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(locator_stat.st_mode)
                or stat.S_IMODE(locator_stat.st_mode) != 0o600
            ):
                return None
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        if len(raw) > 4096:
            return None
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0].strip() != lines[0]:
        return None
    candidate = Path(lines[0])
    return candidate if _valid_repository_root(candidate, installed_script) else None


def _source_root(installed_script: Path) -> Path | None:
    resolved_script = installed_script.resolve()
    try:
        candidate = resolved_script.parents[3]
    except IndexError:
        return None
    expected_script = candidate.joinpath(*EXPECTED_SCRIPT_PARTS)
    if expected_script.resolve() != resolved_script:
        return None
    return candidate if _valid_repository_root(candidate, resolved_script) else None


def _repository_root() -> Path | None:
    locator = SKILL_DIR / ".repository-root"
    try:
        os.lstat(locator)
    except FileNotFoundError:
        return _source_root(SCRIPT_PATH)
    except OSError:
        return None
    return _locator_root(locator, SCRIPT_PATH)


def main(argv: list[str] | None = None) -> int:
    repository_root = _repository_root()
    if repository_root is None:
        print("error: total installation is incomplete", file=sys.stderr)
        return 1
    command = [
        sys.executable,
        "-B",
        str(repository_root / "total_command.py"),
        *(sys.argv[1:] if argv is None else argv),
        "--config",
        str(repository_root / ".todo_archive" / "real_profile.json"),
    ]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
