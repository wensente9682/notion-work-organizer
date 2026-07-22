import unittest
from datetime import date
from decimal import Decimal

from total import InvalidBlock, TotalError, TotalItem, total


class TotalTest(unittest.TestCase):
    def test_sums_completed_blocks_for_category_in_view_order(self):
        rows = [
            TotalItem(date(2026, 7, 3), True, ("research",), "1b"),
            TotalItem(None, True, ("research",), "2b"),
            TotalItem(None, True, ("research",), "3b"),
        ]

        result = total(rows, date(2026, 7, 1))

        self.assertEqual({"research": Decimal("6")}, result.totals)
        self.assertEqual((), result.invalid_blocks)

    def test_unfinished_anchor_defines_section_and_only_target_month_done_rows_count(self):
        rows = [
            TotalItem(date(2026, 8, 1), False, ("research",), "99b"),
            TotalItem(None, True, ("research",), "2b"),
            TotalItem(date(2026, 7, 31), False, ("research",), "88b"),
            TotalItem(None, False, ("research",), "7b"),
            TotalItem(None, True, ("research",), "3b"),
        ]

        result = total(rows, date(2026, 7, 1))

        self.assertEqual({"research": Decimal("3")}, result.totals)
        self.assertEqual((), result.invalid_blocks)

    def test_date_anchors_must_be_non_increasing_and_equal_is_allowed(self):
        duplicate_dates = [
            TotalItem(date(2026, 7, 3), True, ("research",), "1b"),
            TotalItem(date(2026, 7, 3), True, ("research",), "2b"),
        ]
        reversed_dates = [
            TotalItem(date(2026, 7, 2), True, ("research",), "1b"),
            TotalItem(date(2026, 7, 3), True, ("research",), "2b"),
        ]

        self.assertEqual(
            {"research": Decimal("3")},
            total(duplicate_dates, date(2026, 7, 1)).totals,
        )
        with self.assertRaisesRegex(TotalError, "date anchors must be non-increasing"):
            total(reversed_dates, date(2026, 7, 1))

    def test_accepts_supported_decimal_block_formats(self):
        cases = (
            ("2b", "2"),
            ("2B", "2"),
            ("2b+", "2"),
            ("1b!", "1"),
            (".5b", ".5"),
            ("1.5b", "1.5"),
            (".5", ".5"),
            ("1", "1"),
        )

        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = total(
                    [TotalItem(date(2026, 7, 3), True, ("research",), raw)],
                    date(2026, 7, 1),
                )
                self.assertEqual({"research": Decimal(expected)}, result.totals)
                self.assertEqual((), result.invalid_blocks)

    def test_invalid_blocks_are_diagnosed_and_not_counted(self):
        rows = [
            TotalItem(date(2026, 7, 3), True, ("research",), "-1b"),
            TotalItem(None, True, ("research",), "1b 2b"),
            TotalItem(None, True, ("research",), "later"),
            TotalItem(None, True, ("research",), "2b"),
        ]

        result = total(rows, date(2026, 7, 1))

        self.assertEqual({"research": Decimal("2")}, result.totals)
        self.assertEqual(
            (
                InvalidBlock(0, "-1b"),
                InvalidBlock(1, "1b 2b"),
                InvalidBlock(2, "later"),
            ),
            result.invalid_blocks,
        )

    def test_missing_categories_are_ignored_without_diagnostics(self):
        rows = [
            TotalItem(None, True, (), "9b"),
            TotalItem(date(2026, 7, 3), True, (), "not a block"),
            TotalItem(None, True, ("research",), "1b"),
        ]

        result = total(rows, date(2026, 7, 1))

        self.assertEqual({"research": Decimal("1")}, result.totals)
        self.assertEqual((), result.invalid_blocks)

    def test_blank_categories_are_missing_and_valid_names_keep_full_attribution(self):
        rows = [
            TotalItem(date(2026, 7, 3), True, ("", "   "), "not a block"),
            TotalItem(None, True, ("", "research", "  ", "research"), "2b"),
        ]

        result = total(rows, date(2026, 7, 1))

        self.assertEqual({"research": Decimal("2")}, result.totals)
        self.assertEqual((), result.invalid_blocks)

    def test_attributes_full_block_to_each_distinct_category(self):
        rows = [
            TotalItem(
                date(2026, 7, 3),
                True,
                ("research", "admin", "research"),
                "2b",
            ),
        ]

        result = total(rows, date(2026, 7, 1))

        self.assertEqual(
            {"research": Decimal("2"), "admin": Decimal("2")},
            result.totals,
        )

    def test_countable_completed_item_before_first_anchor_fails_closed(self):
        rows = [
            TotalItem(None, True, ("research",), "1b"),
            TotalItem(date(2026, 7, 3), True, ("research",), "2b"),
        ]

        with self.assertRaisesRegex(TotalError, "before first date anchor"):
            total(rows, date(2026, 7, 1))


if __name__ == "__main__":
    unittest.main()
