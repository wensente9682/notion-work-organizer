#!/usr/bin/env python3
"""Local JSON backup helper for todo archive review sessions.

This stores only workflow metadata needed for rollback. It never stores
Notion tokens and never calls the Notion API.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


RETENTION_DAYS = 7
DEFAULT_BACKUP_DIR = Path(".todo_archive/backups")
MAX_BACKUPS = 20
MAX_HINT_LEN = 120
ALLOWED_RECORD_KEYS = {
    "action",
    "batch",
    "category",
    "created_at",
    "ledger_page_id",
    "ledger_url",
    "name_hint",
    "number",
    "operation_id",
    "recorded_at",
    "session_id",
    "source_page_id",
    "source_url",
    "status",
    "target_page_id",
    "target_url",
}
TEXT_HINT_KEYS = {"category", "name_hint", "status"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def shrink_text(value: str, limit: int = MAX_HINT_LEN) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def sanitize_record(record: dict) -> dict:
    compact = {}
    for key, value in record.items():
        if key not in ALLOWED_RECORD_KEYS:
            continue
        if value in (None, ""):
            continue
        if isinstance(value, str) and key in TEXT_HINT_KEYS:
            value = shrink_text(value)
        compact[key] = value
    return compact


def backup_path(backup_dir: Path, session_id: str) -> Path:
    return backup_dir / f"{session_id}.json"


def prune(backup_dir: Path) -> int:
    if not backup_dir.exists():
        return 0
    now = utc_now()
    removed = 0
    for path in backup_dir.glob("*.json"):
        try:
            data = load_json(path)
            expires_at = parse_time(data.get("expires_at", "1970-01-01T00:00:00Z"))
        except Exception:
            expires_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
        if expires_at <= now:
            path.unlink()
            removed += 1
    remaining = sorted(
        backup_dir.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in remaining[MAX_BACKUPS:]:
        path.unlink()
        removed += 1
    return removed


def cmd_begin(args: argparse.Namespace) -> None:
    prune(args.backup_dir)
    now = utc_now()
    session_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    data = {
        "session_id": session_id,
        "label": args.label,
        "created_at": stamp(now),
        "expires_at": stamp(now + timedelta(days=RETENTION_DAYS)),
        "retention_days": RETENTION_DAYS,
        "status": "active",
        "records": [],
        "notes": [],
    }
    save_json(backup_path(args.backup_dir, session_id), data)
    print(session_id)


def read_record(args: argparse.Namespace) -> dict:
    if args.record_json:
        return json.loads(args.record_json)
    raw = sys.stdin.read().strip()
    if not raw:
        raise SystemExit("record JSON required via --record-json or stdin")
    return json.loads(raw)


def cmd_add(args: argparse.Namespace) -> None:
    path = backup_path(args.backup_dir, args.session_id)
    data = load_json(path)
    record = sanitize_record(read_record(args))
    record.setdefault("recorded_at", stamp(utc_now()))
    data.setdefault("records", []).append(record)
    save_json(path, data)
    print(path)


def cmd_note(args: argparse.Namespace) -> None:
    path = backup_path(args.backup_dir, args.session_id)
    data = load_json(path)
    data.setdefault("notes", []).append({"at": stamp(utc_now()), "text": shrink_text(args.text)})
    save_json(path, data)
    print(path)


def cmd_finish(args: argparse.Namespace) -> None:
    path = backup_path(args.backup_dir, args.session_id)
    data = load_json(path)
    data["status"] = args.status
    data["finished_at"] = stamp(utc_now())
    save_json(path, data)
    print(path)


def cmd_list(args: argparse.Namespace) -> None:
    prune(args.backup_dir)
    rows = []
    for path in sorted(args.backup_dir.glob("*.json")) if args.backup_dir.exists() else []:
        data = load_json(path)
        rows.append(
            {
                "session_id": data.get("session_id"),
                "label": data.get("label"),
                "created_at": data.get("created_at"),
                "expires_at": data.get("expires_at"),
                "status": data.get("status"),
                "records": len(data.get("records", [])),
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_show(args: argparse.Namespace) -> None:
    path = backup_path(args.backup_dir, args.session_id)
    print(path.read_text(encoding="utf-8"))


def cmd_prune(args: argparse.Namespace) -> None:
    print(prune(args.backup_dir))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local backup helper for todo archive review.")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    begin = sub.add_parser("begin")
    begin.add_argument("--label", default="todo-test")
    begin.set_defaults(func=cmd_begin)

    add = sub.add_parser("add")
    add.add_argument("session_id")
    add.add_argument("--record-json")
    add.set_defaults(func=cmd_add)

    note = sub.add_parser("note")
    note.add_argument("session_id")
    note.add_argument("text")
    note.set_defaults(func=cmd_note)

    finish = sub.add_parser("finish")
    finish.add_argument("session_id")
    finish.add_argument("--status", choices=["completed", "stopped", "done", "saved"], default="completed")
    finish.set_defaults(func=cmd_finish)

    list_cmd = sub.add_parser("list")
    list_cmd.set_defaults(func=cmd_list)

    show = sub.add_parser("show")
    show.add_argument("session_id")
    show.set_defaults(func=cmd_show)

    prune_cmd = sub.add_parser("prune")
    prune_cmd.set_defaults(func=cmd_prune)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
