from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re


@dataclass(frozen=True)
class TotalItem:
    date_anchor: date | None
    done: bool
    categories: tuple[str, ...]
    timeboxing: str


@dataclass(frozen=True)
class InvalidBlock:
    row_index: int
    value: str


@dataclass(frozen=True)
class TotalResult:
    totals: dict[str, Decimal]
    invalid_blocks: tuple[InvalidBlock, ...]


class TotalError(ValueError):
    pass


def _block_value(raw: str) -> Decimal | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)[bB]?[+!]?", raw.strip())
    if match is None:
        return None
    return Decimal(match.group(1))


def total(ordered_items: list[TotalItem], target_month: date) -> TotalResult:
    totals: dict[str, Decimal] = {}
    invalid_blocks: list[InvalidBlock] = []
    current_date: date | None = None

    for row_index, item in enumerate(ordered_items):
        if item.date_anchor is not None:
            if current_date is not None and item.date_anchor > current_date:
                raise TotalError(f"date anchors must be non-increasing at row {row_index}")
            current_date = item.date_anchor
        if not item.done:
            continue
        categories = tuple(category for category in item.categories if category.strip())
        if not categories:
            continue
        value = _block_value(item.timeboxing)
        if value is None:
            invalid_blocks.append(InvalidBlock(row_index, item.timeboxing))
            continue
        if current_date is None:
            raise TotalError(f"countable item before first date anchor at row {row_index}")
        if (current_date.year, current_date.month) != (target_month.year, target_month.month):
            continue
        for category in dict.fromkeys(categories):
            totals[category] = totals.get(category, Decimal("0")) + value

    return TotalResult(totals=totals, invalid_blocks=tuple(invalid_blocks))
