from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

from total import TotalError
from total_views import NotionViewsApi, TotalViewError, TotalViewFields, total_from_view


DEFAULT_CONFIG = Path(".todo_archive/real_profile.json")


def _token() -> str:
    token = os.environ.get("NOTION_TOKEN", "")
    if token or sys.platform != "darwin":
        return token
    process = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            os.environ.get("USER", ""),
            "-s",
            "codex-notion-token",
            "-w",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return process.stdout.strip() if process.returncode == 0 else ""


def _config(path: Path) -> tuple[str, TotalViewFields]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    total_config = profile.get("total") if isinstance(profile, dict) else None
    if not isinstance(total_config, dict):
        raise ValueError("missing total configuration")
    view_id = total_config.get("view_id")
    fields = total_config.get("fields")
    field_names = ("done", "categories", "timeboxing", "date_anchor")
    if (
        not isinstance(view_id, str)
        or not view_id.strip()
        or not isinstance(fields, dict)
        or any(
            not isinstance(fields.get(name), str) or not fields[name].strip()
            for name in field_names
        )
    ):
        raise ValueError("invalid total configuration")
    relation_names = total_config.get("category_relations", {})
    if not isinstance(relation_names, dict) or any(
        not isinstance(relation_id, str)
        or not relation_id.strip()
        or not isinstance(category, str)
        or not category.strip()
        for relation_id, category in relation_names.items()
    ):
        raise ValueError("invalid category relation configuration")
    return view_id, TotalViewFields(
        done=fields["done"],
        categories=fields["categories"],
        timeboxing=fields["timeboxing"],
        date_anchor=fields["date_anchor"],
        category_relations=relation_names,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only monthly category block totals.")
    parser.add_argument("month")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    dependency_available: bool = False,
    token_loader: Callable[[], str] = _token,
    api_factory: Callable[[str], Any] = NotionViewsApi,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.month) is None:
            raise ValueError
        target_month = date.fromisoformat(f"{args.month}-01")
    except ValueError:
        print("error: total requires a month in YYYY-MM format", file=sys.stderr)
        return 1
    if not dependency_available:
        print(
            "error: total is temporarily unavailable because the Notion Views API dependency is unavailable",
            file=sys.stderr,
        )
        return 1
    try:
        view_id, fields = _config(args.config)
    except (OSError, ValueError, KeyError, TypeError):
        print("error: total configuration is missing or invalid", file=sys.stderr)
        return 1
    token = token_loader()
    if not isinstance(token, str) or not token:
        print("error: total credentials are unavailable", file=sys.stderr)
        return 1
    try:
        result = total_from_view(
            api_factory(token),
            view_id=view_id,
            target_month=target_month,
            fields=fields,
        )
    except (TotalError, TotalViewError):
        print("error: total could not read the configured view safely", file=sys.stderr)
        return 1
    if result.invalid_blocks:
        print(
            "error: total stopped because one or more block values are invalid",
            file=sys.stderr,
        )
        return 1
    print(f"Total for {args.month}")
    if not result.totals:
        print("No category blocks recorded.")
    for category in sorted(result.totals):
        print(f"{category}: {result.totals[category]}b")
    grand_total = sum(result.totals.values(), Decimal("0"))
    print(f"All categories: {grand_total}b")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
