import unittest
import copy
import json
from datetime import date
from decimal import Decimal

from total import TotalError
from total_views import NotionViewsApi, TotalViewError, TotalViewFields, total_from_view


FIELDS = TotalViewFields(
    done="Done",
    categories="Category",
    timeboxing="Blocks",
    date_anchor="Day",
)


def page(*, anchor=None, done=True, categories=("Research",), timeboxing="2b"):
    return {
        "object": "page",
        "id": "page-example",
        "properties": {
            "Done": {"type": "checkbox", "checkbox": done},
            "Category": {
                "type": "multi_select",
                "multi_select": [{"name": name} for name in categories],
            },
            "Blocks": {
                "type": "rich_text",
                "rich_text": [{"type": "text", "plain_text": timeboxing}],
            },
            "Day": {
                "type": "date",
                "date": None if anchor is None else {"start": anchor},
            },
        },
    }


class FakeViewsApi:
    def __init__(self, initial, pages=(), *, add_expires=True):
        self.records = {}
        self._page_number = 0
        self.initial = self._query_response(initial)
        if add_expires and isinstance(self.initial, dict):
            self.initial.setdefault("expires_at", "2026-07-17T08:00:00.000Z")
        self.pages = [self._query_response(response) for response in pages]
        self.calls = []

    def _query_response(self, response):
        if isinstance(response, Exception):
            return response
        response = copy.deepcopy(response)
        if response.get("object") == "list" and response.get("type") == "page":
            response.setdefault("page", {})
        references = []
        for result in response.get("results", []):
            if isinstance(result, dict) and "properties" in result:
                self._page_number += 1
                page_id = f"page-example-{self._page_number}"
                result["id"] = page_id
                self.records[page_id] = result
                references.append({"object": "page", "id": page_id})
            else:
                references.append(result)
        response["results"] = references
        return response

    def create_view_query(self, view_id, *, page_size):
        self.calls.append(("create", view_id, page_size))
        if isinstance(self.initial, Exception):
            raise self.initial
        return self.initial

    def get_view_query_results(self, view_id, query_id, cursor, *, page_size):
        self.calls.append(("results", view_id, query_id, cursor, page_size))
        response = self.pages.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def retrieve_page(self, page_id):
        self.calls.append(("retrieve", page_id))
        return self.records[page_id]


class TotalFromViewTest(unittest.TestCase):
    def test_initial_query_requires_valid_expires_at(self):
        base = {
            "object": "view_query",
            "id": "query-example",
            "view_id": "view-example",
            "total_count": 0,
            "results": [],
            "next_cursor": None,
            "has_more": False,
            "request_status": {"type": "complete"},
        }
        for expires_at in (None, 123, "not-a-date"):
            response = dict(base)
            if expires_at is not None:
                response["expires_at"] = expires_at
            with self.subTest(expires_at=expires_at):
                with self.assertRaisesRegex(TotalViewError, "expires_at"):
                    total_from_view(
                        FakeViewsApi(response, add_expires=False),
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )

    def test_terminal_page_requires_null_next_cursor(self):
        base = {
            "object": "view_query",
            "id": "query-example",
            "view_id": "view-example",
            "total_count": 0,
            "results": [],
            "has_more": False,
            "request_status": {"type": "complete"},
        }
        for cursor in ("missing", "cursor-unexpected", 123):
            response = dict(base)
            if cursor != "missing":
                response["next_cursor"] = cursor
            with self.subTest(cursor=cursor):
                with self.assertRaisesRegex(TotalViewError, "next_cursor"):
                    total_from_view(
                        FakeViewsApi(response),
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )

    def test_paginated_response_requires_empty_page_object(self):
        initial = {
            "object": "view_query",
            "id": "query-example",
            "view_id": "view-example",
            "total_count": 0,
            "results": [],
            "next_cursor": "cursor-one",
            "has_more": True,
            "request_status": {"type": "complete"},
        }
        valid_page = {
            "object": "list",
            "type": "page",
            "page": {},
            "results": [],
            "next_cursor": None,
            "has_more": False,
            "request_status": {"type": "complete"},
        }
        for malformed in ("missing", [], {"unexpected": True}):
            api = FakeViewsApi(initial, pages=[valid_page])
            if malformed == "missing":
                del api.pages[0]["page"]
            else:
                api.pages[0]["page"] = malformed
            with self.subTest(page=malformed):
                with self.assertRaisesRegex(TotalViewError, "page object"):
                    total_from_view(
                        api,
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )

    def test_view_page_references_are_retrieved_read_only_in_view_order(self):
        records = {
            "page-anchor": page(anchor="2026-07-03", done=False, categories=(), timeboxing=""),
            "page-work": page(done=True, timeboxing="2b"),
        }
        records["page-anchor"]["id"] = "page-anchor"
        records["page-work"]["id"] = "page-work"

        class ReferenceApi:
            def __init__(self):
                self.calls = []

            def create_view_query(self, view_id, *, page_size):
                self.calls.append(("create", view_id))
                return {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "expires_at": "2026-07-17T08:00:00.000Z",
                    "total_count": 2,
                    "results": [
                        {"object": "page", "id": "page-anchor"},
                        {"object": "page", "id": "page-work"},
                    ],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }

            def retrieve_page(self, page_id):
                self.calls.append(("retrieve", page_id))
                return records[page_id]

        api = ReferenceApi()
        result = total_from_view(
            api,
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({"Research": Decimal("2")}, result.totals)
        self.assertEqual(
            [
                ("create", "view-example"),
                ("retrieve", "page-anchor"),
                ("retrieve", "page-work"),
            ],
            api.calls,
        )

    def test_repeated_page_reference_fails_closed(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "expires_at": "2026-07-17T08:00:00.000Z",
                "total_count": 2,
                "results": [
                    {"object": "page", "id": "page-repeated"},
                    {"object": "page", "id": "page-repeated"},
                ],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
        )

        with self.assertRaisesRegex(TotalViewError, "repeated page reference"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_single_page_uses_explicit_view_and_returns_category_totals(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 2,
                "results": [
                    page(anchor="2026-07-03", done=False, categories=(), timeboxing=""),
                    page(anchor=None, done=True, categories=("Research",), timeboxing="2b"),
                ],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
        )

        result = total_from_view(
            api,
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({"Research": Decimal("2")}, result.totals)
        self.assertEqual((), result.invalid_blocks)
        self.assertEqual(
            [
                ("create", "view-example", 100),
                ("retrieve", "page-example-1"),
                ("retrieve", "page-example-2"),
            ],
            api.calls,
        )

    def test_multiple_pages_keep_query_identity_order_and_anchor_inheritance(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 3,
                "results": [
                    page(anchor="2026-07-03", done=False, categories=(), timeboxing=""),
                    page(anchor=None, done=True, categories=("Research",), timeboxing="1b"),
                ],
                "next_cursor": "cursor-one",
                "has_more": True,
                "request_status": {"type": "complete"},
            },
            pages=[
                {
                    "object": "list",
                    "type": "page",
                    "results": [
                        page(anchor=None, done=True, categories=("Research",), timeboxing="2b"),
                    ],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }
            ],
        )

        result = total_from_view(
            api,
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({"Research": Decimal("3")}, result.totals)
        self.assertEqual(
            [
                ("create", "view-example", 100),
                ("results", "view-example", "query-example", "cursor-one", 100),
                ("retrieve", "page-example-1"),
                ("retrieve", "page-example-2"),
                ("retrieve", "page-example-3"),
            ],
            api.calls,
        )

    def test_more_results_without_cursor_fails_closed(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [page(anchor="2026-07-03", done=False, categories=())],
                "next_cursor": None,
                "has_more": True,
                "request_status": {"type": "complete"},
            }
        )

        with self.assertRaisesRegex(TotalViewError, "next_cursor"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_repeated_cursor_fails_closed(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 2,
                "results": [page(anchor="2026-07-03", done=False, categories=())],
                "next_cursor": "cursor-one",
                "has_more": True,
                "request_status": {"type": "complete"},
            },
            pages=[
                {
                    "object": "list",
                    "type": "page",
                    "results": [page(anchor=None, done=True, timeboxing="2b")],
                    "next_cursor": "cursor-one",
                    "has_more": True,
                    "request_status": {"type": "complete"},
                }
            ],
        )

        with self.assertRaisesRegex(TotalViewError, "repeated cursor"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_pagination_failure_does_not_return_partial_totals(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 2,
                "results": [page(anchor="2026-07-03", done=True, timeboxing="1b")],
                "next_cursor": "cursor-one",
                "has_more": True,
                "request_status": {"type": "complete"},
            },
            pages=[PermissionError("denied")],
        )

        with self.assertRaisesRegex(TotalViewError, "view query request failed"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_incomplete_or_truncated_query_fails_closed(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [page(anchor="2026-07-03", done=True, timeboxing="1b")],
                "next_cursor": None,
                "has_more": False,
                "request_status": {
                    "type": "incomplete",
                    "incomplete_reason": "query_result_limit_reached",
                },
            }
        )

        with self.assertRaisesRegex(TotalViewError, "not complete"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_missing_or_changed_view_query_identity_fails_closed(self):
        base = {
            "object": "view_query",
            "id": "query-example",
            "view_id": "view-example",
            "total_count": 0,
            "results": [],
            "next_cursor": None,
            "has_more": False,
            "request_status": {"type": "complete"},
        }
        bad_initials = []
        missing_query = dict(base)
        del missing_query["id"]
        bad_initials.append(missing_query)
        wrong_view = dict(base)
        wrong_view["view_id"] = "other-view"
        bad_initials.append(wrong_view)

        for initial in bad_initials:
            with self.subTest(initial=initial):
                with self.assertRaisesRegex(TotalViewError, "identity"):
                    total_from_view(
                        FakeViewsApi(initial),
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )

        paginated = dict(base)
        paginated.update(
            total_count=1,
            has_more=True,
            next_cursor="cursor-one",
        )
        api = FakeViewsApi(
            paginated,
            pages=[
                {
                    "object": "list",
                    "type": "page",
                    "query_id": "other-query",
                    "results": [page(anchor="2026-07-03", done=False, categories=())],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }
            ],
        )
        with self.assertRaisesRegex(TotalViewError, "identity"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_result_count_mismatch_fails_closed(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 2,
                "results": [page(anchor="2026-07-03", done=True, timeboxing="1b")],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
        )

        with self.assertRaisesRegex(TotalViewError, "result count"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_view_id_and_initial_access_fail_closed(self):
        empty_id_api = FakeViewsApi({})
        with self.assertRaisesRegex(TotalViewError, "view_id"):
            total_from_view(
                empty_id_api,
                view_id="   ",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )
        self.assertEqual([], empty_id_api.calls)

        with self.assertRaisesRegex(TotalViewError, "view query request failed"):
            total_from_view(
                FakeViewsApi(PermissionError("not accessible")),
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_only_structured_date_values_and_mentions_form_anchors(self):
        mention_anchor = page(done=False, categories=(), timeboxing="")
        mention_anchor["properties"]["Day"] = {
            "type": "rich_text",
            "rich_text": [
                {
                    "type": "mention",
                    "plain_text": "July 3, 2026",
                    "mention": {
                        "type": "date",
                        "date": {"start": "2026-07-03"},
                    },
                }
            ],
        }
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 2,
                "results": [mention_anchor, page(done=True, timeboxing="2b")],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
        )
        self.assertEqual(
            {"Research": Decimal("2")},
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            ).totals,
        )

        text_only = page(done=True, timeboxing="2b")
        text_only["created_time"] = "2026-07-03T10:00:00Z"
        text_only["last_edited_time"] = "2026-07-03T11:00:00Z"
        text_only["properties"]["Day"] = {
            "type": "rich_text",
            "rich_text": [
                {"type": "text", "plain_text": "2026-07-03"},
            ],
        }
        text_api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [text_only],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
        )
        with self.assertRaisesRegex(TotalError, "before first date anchor"):
            total_from_view(
                text_api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_unsupported_or_malformed_date_anchor_fails_closed(self):
        malformed_properties = [
            {"type": "number", "number": 20260703},
            {
                "type": "rich_text",
                "rich_text": [
                    {
                        "type": "mention",
                        "mention": {"type": "date"},
                    }
                ],
            },
            {"type": "rich_text", "rich_text": "2026-07-03"},
        ]
        for date_property in malformed_properties:
            row = page(done=True, timeboxing="2b")
            row["properties"]["Day"] = date_property
            response = {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [row],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
            with self.subTest(date=date_property):
                with self.assertRaisesRegex(TotalViewError, "date anchor"):
                    total_from_view(
                        FakeViewsApi(response),
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )

    def test_anchor_order_is_checked_before_aggregation_and_equal_dates_are_valid(self):
        def response(rows):
            return {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": len(rows),
                "results": rows,
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }

        reversed_rows = [
            page(anchor="2026-07-02", done=False, categories=()),
            page(anchor="2026-07-03", done=False, categories=()),
        ]
        with self.assertRaisesRegex(TotalViewError, "before aggregation"):
            total_from_view(
                FakeViewsApi(response(reversed_rows)),
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

        equal_rows = [
            page(anchor="2026-07-03", done=False, categories=()),
            page(anchor="2026-07-03", done=False, categories=()),
        ]
        self.assertEqual(
            {},
            total_from_view(
                FakeViewsApi(response(equal_rows)),
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            ).totals,
        )

    def test_field_mapping_supports_the_public_select_category_schema(self):
        selected = page(anchor="2026-07-03", done=True, timeboxing="2b")
        selected["properties"]["Category"] = {
            "type": "select",
            "select": {"name": "Research"},
        }
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [selected],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
        )

        self.assertEqual(
            {"Research": Decimal("2")},
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            ).totals,
        )

    def test_unsupported_or_malformed_category_payload_fails_closed(self):
        malformed_properties = [
            {"type": "status", "status": {"name": "Research"}},
            {"type": "multi_select"},
            {"type": "multi_select", "multi_select": "Research"},
            {"type": "multi_select", "multi_select": [{}]},
            {"type": "multi_select", "multi_select": ["Research"]},
        ]
        for category_property in malformed_properties:
            row = page(anchor="2026-07-03", done=True, timeboxing="2b")
            row["properties"]["Category"] = category_property
            response = {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [row],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
            with self.subTest(category=category_property):
                with self.assertRaisesRegex(TotalViewError, "category field"):
                    total_from_view(
                        FakeViewsApi(response),
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )

    def test_supported_empty_category_payloads_are_ignored(self):
        select_empty = page(anchor="2026-07-03", done=True, timeboxing="not a block")
        select_empty["properties"]["Category"] = {"type": "select", "select": None}
        multi_empty = page(done=True, timeboxing="not a block")
        multi_empty["properties"]["Category"] = {
            "type": "multi_select",
            "multi_select": [],
        }
        relation_empty = page(done=True, timeboxing="not a block")
        relation_empty["properties"]["Category"] = {
            "type": "relation",
            "relation": [],
            "has_more": False,
        }
        response = {
            "object": "view_query",
            "id": "query-example",
            "view_id": "view-example",
            "total_count": 3,
            "results": [select_empty, multi_empty, relation_empty],
            "next_cursor": None,
            "has_more": False,
            "request_status": {"type": "complete"},
        }

        result = total_from_view(
            FakeViewsApi(response),
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({}, result.totals)
        self.assertEqual((), result.invalid_blocks)

    def test_malformed_timeboxing_rich_text_payload_fails_closed(self):
        malformed_properties = [
            {"type": "rich_text"},
            {"type": "rich_text", "rich_text": "2b"},
            {"type": "rich_text", "rich_text": [{}]},
            {"type": "rich_text", "rich_text": ["2b"]},
        ]
        for timeboxing_property in malformed_properties:
            row = page(anchor="2026-07-03", done=True, timeboxing="2b")
            row["properties"]["Blocks"] = timeboxing_property
            response = {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [row],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
            with self.subTest(timeboxing=timeboxing_property):
                with self.assertRaisesRegex(TotalViewError, "timeboxing field"):
                    total_from_view(
                        FakeViewsApi(response),
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )

    def test_empty_timeboxing_rich_text_payload_uses_core_invalid_block_semantics(self):
        row = page(anchor="2026-07-03", done=True, timeboxing="2b")
        row["properties"]["Blocks"] = {"type": "rich_text", "rich_text": []}
        response = {
            "object": "view_query",
            "id": "query-example",
            "view_id": "view-example",
            "total_count": 1,
            "results": [row],
            "next_cursor": None,
            "has_more": False,
            "request_status": {"type": "complete"},
        }

        result = total_from_view(
            FakeViewsApi(response),
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({}, result.totals)
        self.assertEqual(1, len(result.invalid_blocks))
        self.assertEqual("", result.invalid_blocks[0].value)

    def test_relation_categories_require_explicit_mapping(self):
        fields = TotalViewFields(
            done="Done",
            categories="Category",
            timeboxing="Blocks",
            date_anchor="Day",
            category_relations={"project-a": "Research", "project-b": "Writing"},
        )
        related = page(anchor="2026-07-03", done=True, timeboxing="2b")
        related["properties"]["Category"] = {
            "type": "relation",
            "relation": [{"id": "project-a"}, {"id": "project-b"}],
        }
        response = {
            "object": "view_query",
            "id": "query-example",
            "view_id": "view-example",
            "total_count": 1,
            "results": [related],
            "next_cursor": None,
            "has_more": False,
            "request_status": {"type": "complete"},
        }
        self.assertEqual(
            {"Research": Decimal("2"), "Writing": Decimal("2")},
            total_from_view(
                FakeViewsApi(response),
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=fields,
            ).totals,
        )

        fields_without_mapping = TotalViewFields(
            done="Done",
            categories="Category",
            timeboxing="Blocks",
            date_anchor="Day",
        )
        with self.assertRaisesRegex(TotalViewError, "unmapped category relation"):
            total_from_view(
                FakeViewsApi(response),
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=fields_without_mapping,
            )

    def test_truncated_relation_category_fails_closed(self):
        fields = TotalViewFields(
            done="Done",
            categories="Category",
            timeboxing="Blocks",
            date_anchor="Day",
            category_relations={"project-a": "Research"},
        )
        related = page(anchor="2026-07-03", done=True, timeboxing="2b")
        related["properties"]["Category"] = {
            "type": "relation",
            "relation": [{"id": "project-a"}],
            "has_more": True,
        }
        response = {
            "object": "view_query",
            "id": "query-example",
            "view_id": "view-example",
            "expires_at": "2026-07-17T08:00:00.000Z",
            "total_count": 1,
            "results": [related],
            "next_cursor": None,
            "has_more": False,
            "request_status": {"type": "complete"},
        }

        with self.assertRaisesRegex(TotalViewError, "truncated relation"):
            total_from_view(
                FakeViewsApi(response),
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=fields,
            )

    def test_concrete_api_uses_only_read_semantic_post_and_get_requests(self):
        anchor_record = page(anchor="2026-07-03", done=False, categories=(), timeboxing="")
        anchor_record["id"] = "page-anchor"
        work_record = page(done=True, timeboxing="2b")
        work_record["id"] = "page-work"
        responses = [
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "expires_at": "2026-07-17T08:00:00.000Z",
                "total_count": 2,
                "results": [{"object": "page", "id": "page-anchor"}],
                "next_cursor": "cursor-one",
                "has_more": True,
                "request_status": {"type": "complete"},
            },
            {
                "object": "list",
                "type": "page",
                "page": {},
                "results": [{"object": "page", "id": "page-work"}],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            },
            anchor_record,
            work_record,
        ]
        requests = []

        class HttpResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def opener(request, timeout):
            requests.append(request)
            return HttpResponse(responses.pop(0))

        result = total_from_view(
            NotionViewsApi("token-example", opener=opener),
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({"Research": Decimal("2")}, result.totals)
        self.assertEqual(
            ["POST", "GET", "GET", "GET"],
            [request.method for request in requests],
        )
        self.assertEqual(
            "https://api.notion.com/v1/views/view-example/queries",
            requests[0].full_url,
        )
        self.assertEqual({"page_size": 100}, json.loads(requests[0].data))
        self.assertEqual(
            "https://api.notion.com/v1/views/view-example/queries/query-example"
            "?start_cursor=cursor-one&page_size=100",
            requests[1].full_url,
        )
        self.assertEqual(
            "https://api.notion.com/v1/pages/page-anchor",
            requests[2].full_url,
        )
        self.assertEqual(
            "https://api.notion.com/v1/pages/page-work",
            requests[3].full_url,
        )

    def test_missing_or_wrong_mapped_page_fields_fail_closed(self):
        missing_done = page(anchor="2026-07-03", done=True)
        del missing_done["properties"]["Done"]
        wrong_done = page(anchor="2026-07-03", done=True)
        wrong_done["properties"]["Done"] = {
            "type": "status",
            "status": {"name": "Done"},
        }

        for row in (missing_done, wrong_done):
            with self.subTest(row=row):
                response = {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "total_count": 1,
                    "results": [row],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }
                with self.assertRaisesRegex(TotalViewError, "page field contract"):
                    total_from_view(
                        FakeViewsApi(response),
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )

    def test_pagination_response_shape_must_match_views_contract(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [],
                "next_cursor": "cursor-one",
                "has_more": True,
                "request_status": {"type": "complete"},
            },
            pages=[
                {
                    "object": "list",
                    "type": "database",
                    "results": [page(anchor="2026-07-03", done=False, categories=())],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }
            ],
        )

        with self.assertRaisesRegex(TotalViewError, "response contract"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )


if __name__ == "__main__":
    unittest.main()
