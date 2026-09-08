import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import total_command
from total_views import TotalViewError


def notion_page(page_id, *, anchor=None, categories=("Research",), blocks="2b"):
    return {
        "object": "page",
        "id": page_id,
        "properties": {
            "Done": {"type": "checkbox", "checkbox": True},
            "Category": {
                "type": "multi_select",
                "multi_select": [{"name": name} for name in categories],
            },
            "Blocks": {
                "type": "rich_text",
                "rich_text": [{"type": "text", "plain_text": blocks}],
            },
            "Day": {
                "type": "date",
                "date": None if anchor is None else {"start": anchor},
            },
        },
    }


class FakeReadApi:
    def __init__(self, records):
        self.records = {record["id"]: record for record in records}
        self.calls = []

    def create_view_query(self, view_id, *, page_size):
        self.calls.append(("create", view_id, page_size))
        return {
            "object": "view_query",
            "id": "query-example",
            "view_id": view_id,
            "expires_at": "2026-07-17T08:00:00.000Z",
            "total_count": len(self.records),
            "results": [
                {"object": "page", "id": page_id} for page_id in self.records
            ],
            "next_cursor": None,
            "has_more": False,
            "request_status": {"type": "complete"},
        }

    def retrieve_page(self, page_id):
        self.calls.append(("retrieve", page_id))
        return self.records[page_id]


class TotalCommandTest(unittest.TestCase):
    def write_config(self, directory, total_config):
        path = Path(directory) / "profile.local.json"
        path.write_text(json.dumps({"total": total_config}), encoding="utf-8")
        return path

    def run_command(self, argv, *, api, token="token-example"):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = total_command.main(
                argv,
                token_loader=lambda: token,
                api_factory=lambda supplied_token: api,
                dependency_available=True,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_total_prints_sorted_category_subtotals_and_grand_total(self):
        records = [
            notion_page("page-anchor", anchor="2026-07-03", categories=("Writing",), blocks="1b"),
            notion_page("page-work", categories=("Research", "Writing"), blocks="2b"),
        ]
        api = FakeReadApi(records)
        with tempfile.TemporaryDirectory() as directory:
            config = self.write_config(
                directory,
                {
                    "view_id": "view-example",
                    "fields": {
                        "done": "Done",
                        "categories": "Category",
                        "timeboxing": "Blocks",
                        "date_anchor": "Day",
                    },
                },
            )
            code, stdout, stderr = self.run_command(
                ["2026-07", "--config", str(config)], api=api
            )

        self.assertEqual(0, code)
        self.assertEqual(
            "Total for 2026-07\n"
            "Research: 2b\n"
            "Writing: 3b\n"
            "All categories: 5b\n",
            stdout,
        )
        self.assertEqual("", stderr)
        self.assertEqual(
            [
                ("create", "view-example", 100),
                ("retrieve", "page-anchor"),
                ("retrieve", "page-work"),
            ],
            api.calls,
        )

    def test_total_reports_an_empty_result_without_item_details(self):
        api = FakeReadApi([])
        with tempfile.TemporaryDirectory() as directory:
            config = self.write_config(
                directory,
                {
                    "view_id": "view-example",
                    "fields": {
                        "done": "Done",
                        "categories": "Category",
                        "timeboxing": "Blocks",
                        "date_anchor": "Day",
                    },
                },
            )
            code, stdout, stderr = self.run_command(
                ["2026-07", "--config", str(config)], api=api
            )

        self.assertEqual(0, code)
        self.assertEqual(
            "Total for 2026-07\n"
            "No category blocks recorded.\n"
            "All categories: 0b\n",
            stdout,
        )
        self.assertEqual("", stderr)

    def test_missing_or_invalid_month_stops_before_config_credentials_or_api(self):
        def unexpected_call(*args, **kwargs):
            raise AssertionError("external boundary must not be called")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                total_command.main(
                    [],
                    token_loader=unexpected_call,
                    api_factory=unexpected_call,
                )
        self.assertIn("month", stderr.getvalue())

        for invalid_month in (
            "0000-01",
            "2026",
            "2026-7",
            "2026-13",
            "July-2026",
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.subTest(month=invalid_month):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = total_command.main(
                        [invalid_month, "--config", "/missing/private-profile.json"],
                        token_loader=unexpected_call,
                        api_factory=unexpected_call,
                    )
                self.assertEqual(1, code)
                self.assertEqual("", stdout.getvalue())
                self.assertEqual(
                    "error: total requires a month in YYYY-MM format\n",
                    stderr.getvalue(),
                )

    def test_total_is_temporarily_unavailable_before_config_credentials_or_api(self):
        def unexpected_call(*args, **kwargs):
            raise AssertionError("unavailable gate must stop before this boundary")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with patch.object(total_command, "_config", unexpected_call):
                code = total_command.main(
                    ["2026-08"],
                    token_loader=unexpected_call,
                    api_factory=unexpected_call,
                )

        self.assertEqual(1, code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(
            "error: total is temporarily unavailable because the Notion Views API dependency is unavailable\n",
            stderr.getvalue(),
        )

    def test_missing_or_malformed_private_config_fails_closed_without_api(self):
        def unexpected_api(token):
            raise AssertionError("API must not be constructed")

        malformed_profiles = [
            {},
            {"total": {}},
            {
                "total": {
                    "view_id": "",
                    "fields": {
                        "done": "Done",
                        "categories": "Category",
                        "timeboxing": "Blocks",
                        "date_anchor": "Day",
                    },
                }
            },
            {
                "total": {
                    "view_id": "private-view-value",
                    "fields": {"done": "Done"},
                }
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "missing.local.json"]
            for index, profile in enumerate(malformed_profiles):
                path = Path(directory) / f"profile-{index}.local.json"
                path.write_text(json.dumps(profile), encoding="utf-8")
                paths.append(path)

            for path in paths:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with self.subTest(path=path.name):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = total_command.main(
                            ["2026-07", "--config", str(path)],
                            token_loader=lambda: "token-example",
                            api_factory=unexpected_api,
                            dependency_available=True,
                        )
                    self.assertEqual(1, code)
                    self.assertEqual("", stdout.getvalue())
                    self.assertEqual(
                        "error: total configuration is missing or invalid\n",
                        stderr.getvalue(),
                    )
                    self.assertNotIn(str(path), stderr.getvalue())
                    self.assertNotIn("private-view-value", stderr.getvalue())

    def test_missing_credentials_fails_closed_before_api_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.write_config(
                directory,
                {
                    "view_id": "view-example",
                    "fields": {
                        "done": "Done",
                        "categories": "Category",
                        "timeboxing": "Blocks",
                        "date_anchor": "Day",
                    },
                },
            )
            code, stdout, stderr = self.run_command(
                ["2026-07", "--config", str(config)], api=None, token=""
            )

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual("error: total credentials are unavailable\n", stderr)

    def test_invalid_block_stops_without_partial_or_private_output(self):
        private_block = "private task estimate is unclear"
        record = notion_page(
            "private-page-id", anchor="2026-07-03", blocks=private_block
        )
        record["url"] = "https://example.invalid/private-page"
        record["properties"]["Task"] = {
            "type": "title",
            "title": [{"plain_text": "private task title"}],
        }
        api = FakeReadApi([record])
        with tempfile.TemporaryDirectory() as directory:
            config = self.write_config(
                directory,
                {
                    "view_id": "private-view-id",
                    "fields": {
                        "done": "Done",
                        "categories": "Category",
                        "timeboxing": "Blocks",
                        "date_anchor": "Day",
                    },
                },
            )
            code, stdout, stderr = self.run_command(
                ["2026-07", "--config", str(config)],
                api=api,
                token="private-token",
            )

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            "error: total stopped because one or more block values are invalid\n",
            stderr,
        )
        for private_value in (
            private_block,
            "private task title",
            "private-page-id",
            "private-view-id",
            "private-token",
            "https://example.invalid/private-page",
        ):
            self.assertNotIn(private_value, stdout + stderr)

    def test_view_failure_is_sanitized_and_returns_no_partial_totals(self):
        class FailingReadApi:
            def create_view_query(self, view_id, *, page_size):
                raise TotalViewError(
                    "failed for private-view-id and private-page-id"
                )

        with tempfile.TemporaryDirectory() as directory:
            config = self.write_config(
                directory,
                {
                    "view_id": "private-view-id",
                    "fields": {
                        "done": "Done",
                        "categories": "Category",
                        "timeboxing": "Blocks",
                        "date_anchor": "Day",
                    },
                },
            )
            code, stdout, stderr = self.run_command(
                ["2026-07", "--config", str(config)],
                api=FailingReadApi(),
                token="private-token",
            )

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            "error: total could not read the configured view safely\n",
            stderr,
        )
        self.assertNotIn("private-view-id", stdout + stderr)
        self.assertNotIn("private-page-id", stdout + stderr)
        self.assertNotIn("private-token", stdout + stderr)

    def test_core_fail_closed_error_is_sanitized_without_partial_totals(self):
        api = FakeReadApi(
            [notion_page("private-page-id", anchor=None, blocks="2b")]
        )
        with tempfile.TemporaryDirectory() as directory:
            config = self.write_config(
                directory,
                {
                    "view_id": "private-view-id",
                    "fields": {
                        "done": "Done",
                        "categories": "Category",
                        "timeboxing": "Blocks",
                        "date_anchor": "Day",
                    },
                },
            )
            code, stdout, stderr = self.run_command(
                ["2026-07", "--config", str(config)],
                api=api,
                token="private-token",
            )

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            "error: total could not read the configured view safely\n",
            stderr,
        )
        self.assertNotIn("private-page-id", stdout + stderr)
        self.assertNotIn("private-view-id", stdout + stderr)
        self.assertNotIn("private-token", stdout + stderr)


if __name__ == "__main__":
    unittest.main()
