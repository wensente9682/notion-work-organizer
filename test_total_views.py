import unittest
import copy
import json
import ssl
import urllib.error
from datetime import date
from decimal import Decimal
from unittest.mock import patch

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


class JsonHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


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
    def test_nested_success_on_same_client_restores_outer_deadline_budget(self):
        now = [0.0]
        in_inner = [False]
        outer_query_sent = [False]
        timeouts = []
        api = None

        def opener(request, timeout):
            nonlocal api
            if request.method == "POST":
                if in_inner[0]:
                    timeouts.append(("inner-query", timeout))
                    return JsonHttpResponse(
                        {
                            "object": "view_query",
                            "id": "inner-query",
                            "view_id": "view-example",
                            "expires_at": "2026-07-17T08:00:00.000Z",
                            "total_count": 0,
                            "results": [],
                            "next_cursor": None,
                            "has_more": False,
                            "request_status": {"type": "complete"},
                        }
                    )
                outer_query_sent[0] = True
                timeouts.append(("outer-query", timeout))
                return JsonHttpResponse(
                    {
                        "object": "view_query",
                        "id": "outer-query",
                        "view_id": "view-example",
                        "expires_at": "2026-07-17T08:00:00.000Z",
                        "total_count": 2,
                        "results": [{"object": "page", "id": "page-one"}],
                        "next_cursor": "cursor-one",
                        "has_more": True,
                        "request_status": {"type": "complete"},
                    }
                )
            if "/v1/pages/page-one" in request.full_url:
                timeouts.append(("outer-page-one", timeout))
                in_inner[0] = True
                try:
                    nested = total_from_view(
                        api,
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )
                finally:
                    in_inner[0] = False
                self.assertEqual({}, nested.totals)
                now[0] = 179.0
                row = page(anchor="2026-08-01", done=False, categories=())
                row["id"] = "page-one"
                return JsonHttpResponse(row)
            if "/v1/pages/page-two" in request.full_url:
                timeouts.append(("outer-page-two", timeout))
                row = page(anchor="2026-06-30", done=False, categories=())
                row["id"] = "page-two"
                return JsonHttpResponse(row)
            timeouts.append(("outer-pagination", timeout))
            return JsonHttpResponse(
                {
                    "object": "list",
                    "type": "page",
                    "page": {},
                    "results": [{"object": "page", "id": "page-two"}],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }
            )

        api = NotionViewsApi("token-example", opener=opener)
        with patch("total_views.time.monotonic", side_effect=lambda: now[0]):
            result = total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

        self.assertTrue(outer_query_sent[0])
        self.assertEqual({}, result.totals)
        self.assertAlmostEqual(1.0, dict(timeouts)["outer-pagination"])
        self.assertAlmostEqual(1.0, dict(timeouts)["outer-page-two"])

    def test_nested_failure_on_same_client_restores_outer_attempt_budget(self):
        references = [
            {"object": "page", "id": f"page-{number:03d}"}
            for number in range(1, 302)
        ]
        page_calls = []
        in_inner = [False]
        api = None

        def page_of_results(start, end, *, cursor, has_more):
            return {
                "object": "list",
                "type": "page",
                "page": {},
                "results": references[start:end],
                "next_cursor": cursor,
                "has_more": has_more,
                "request_status": {"type": "complete"},
            }

        paginated = [
            page_of_results(100, 200, cursor="cursor-two", has_more=True),
            page_of_results(200, 300, cursor="cursor-three", has_more=True),
            page_of_results(300, 301, cursor=None, has_more=False),
        ]

        def opener(request, timeout):
            nonlocal api
            if request.method == "POST":
                if in_inner[0]:
                    raise PermissionError("nested query failed")
                return JsonHttpResponse(
                    {
                        "object": "view_query",
                        "id": "outer-query",
                        "view_id": "view-example",
                        "expires_at": "2026-07-17T08:00:00.000Z",
                        "total_count": 301,
                        "results": references[:100],
                        "next_cursor": "cursor-one",
                        "has_more": True,
                        "request_status": {"type": "complete"},
                    }
                )
            if "/v1/pages/" not in request.full_url:
                return JsonHttpResponse(paginated.pop(0))
            page_id = request.full_url.rsplit("/", 1)[-1]
            page_calls.append(page_id)
            if page_id == "page-001":
                in_inner[0] = True
                try:
                    with self.assertRaisesRegex(TotalViewError, "request failed"):
                        total_from_view(
                            api,
                            view_id="view-example",
                            target_month=date(2026, 7, 1),
                            fields=FIELDS,
                        )
                finally:
                    in_inner[0] = False
            row = page(
                anchor="2026-08-31" if page_id == "page-001" else None,
                done=False,
                categories=(),
            )
            row["id"] = page_id
            return JsonHttpResponse(row)

        api = NotionViewsApi("token-example", opener=opener)
        with self.assertRaisesRegex(TotalViewError, "resource limit"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

        self.assertEqual(300, len(page_calls))
        self.assertEqual("page-300", page_calls[-1])

    def test_page_attempt_is_not_counted_when_deadline_expires_before_opener(self):
        after_create = [False]
        after_create_times = iter((179.0, 179.9, 180.0))
        opener_calls = []

        def clock():
            return next(after_create_times) if after_create[0] else 0.0

        class DeadlineEdgeApi(NotionViewsApi):
            def __init__(self):
                super().__init__("token-example", opener=self._open)
                self.page_budget = None

            def _open(self, request, timeout):
                opener_calls.append((request, timeout))
                raise AssertionError("expired attempt must not reach opener")

            def create_view_query(self, view_id, *, page_size):
                after_create[0] = True
                return {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "expires_at": "2026-07-17T08:00:00.000Z",
                    "total_count": 1,
                    "results": [{"object": "page", "id": "page-example"}],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }

            def retrieve_page(self, page_id):
                self.page_budget = self._total_budget
                return super().retrieve_page(page_id)

        api = DeadlineEdgeApi()
        with patch("total_views.time.monotonic", side_effect=clock):
            with self.assertRaisesRegex(TotalViewError, "resource limit"):
                total_from_view(
                    api,
                    view_id="view-example",
                    target_month=date(2026, 7, 1),
                    fields=FIELDS,
                )

        self.assertEqual([], opener_calls)
        self.assertIsNotNone(api.page_budget)
        self.assertEqual(0, api.page_budget.page_get_attempts)

    def test_page_attempt_count_includes_failed_network_calls_and_retries(self):
        record = page(anchor="2026-07-31", done=False, categories=())
        record["id"] = "page-example"

        class AttemptApi(NotionViewsApi):
            def __init__(self, outcomes):
                self.outcomes = list(outcomes)
                self.page_budget = None
                self.opener_calls = 0
                super().__init__("token-example", opener=self._open, sleeper=lambda _: None)

            def _open(self, request, timeout):
                self.opener_calls += 1
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return JsonHttpResponse(outcome)

            def create_view_query(self, view_id, *, page_size):
                return {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "expires_at": "2026-07-17T08:00:00.000Z",
                    "total_count": 1,
                    "results": [{"object": "page", "id": "page-example"}],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }

            def retrieve_page(self, page_id):
                self.page_budget = self._total_budget
                return super().retrieve_page(page_id)

        non_transient = AttemptApi(
            [
                urllib.error.HTTPError(
                    "https://example.invalid/private-page",
                    400,
                    "invalid",
                    {},
                    None,
                )
            ]
        )
        with self.assertRaisesRegex(TotalViewError, "page retrieve request failed"):
            total_from_view(
                non_transient,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )
        self.assertEqual(1, non_transient.opener_calls)
        self.assertEqual(1, non_transient.page_budget.page_get_attempts)

        recovered = AttemptApi(
            [
                urllib.error.URLError(ssl.SSLEOFError("transient TLS EOF")),
                record,
            ]
        )
        result = total_from_view(
            recovered,
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )
        self.assertEqual({}, result.totals)
        self.assertEqual(2, recovered.opener_calls)
        self.assertEqual(2, recovered.page_budget.page_get_attempts)

    def test_month_scan_stops_after_query_exhausts_the_monotonic_deadline(self):
        now = [0.0]

        class SlowQueryApi:
            def __init__(self):
                self.calls = []

            def create_view_query(self, view_id, *, page_size):
                self.calls.append(("create", view_id, page_size))
                now[0] = 180.0
                return {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "expires_at": "2026-07-17T08:00:00.000Z",
                    "total_count": 1,
                    "results": [{"object": "page", "id": "page-example"}],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }

            def retrieve_page(self, page_id):
                self.calls.append(("retrieve", page_id))
                raise AssertionError("deadline must stop before hydration")

        api = SlowQueryApi()
        with patch("total_views.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(TotalViewError, "resource limit"):
                total_from_view(
                    api,
                    view_id="view-example",
                    target_month=date(2026, 7, 1),
                    fields=FIELDS,
                )

        self.assertEqual([("create", "view-example", 100)], api.calls)

    def test_month_scan_bounds_page_network_timeout_by_remaining_deadline(self):
        now = [0.0]
        timeouts = []
        record = page(anchor="2026-07-31", timeboxing="1b")
        record["id"] = "page-example"
        responses = [
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "expires_at": "2026-07-17T08:00:00.000Z",
                "total_count": 1,
                "results": [{"object": "page", "id": "page-example"}],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            },
            record,
        ]

        def opener(request, timeout):
            timeouts.append(timeout)
            response = responses.pop(0)
            now[0] = 179.5 if request.method == "POST" else 180.0
            return JsonHttpResponse(response)

        with patch("total_views.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(TotalViewError, "resource limit"):
                total_from_view(
                    NotionViewsApi("token-example", opener=opener),
                    view_id="view-example",
                    target_month=date(2026, 7, 1),
                    fields=FIELDS,
                )

        self.assertEqual(30, timeouts[0])
        self.assertAlmostEqual(0.5, timeouts[1])

    def test_month_scan_counts_failed_page_attempt_before_blocking_its_retry(self):
        page_calls = []
        references = [
            {"object": "page", "id": f"page-{number:03d}"}
            for number in range(1, 301)
        ]

        def query_payload(start, end, *, cursor, has_more, initial=False):
            payload = {
                "object": "view_query" if initial else "list",
                "id": "query-example" if initial else None,
                "view_id": "view-example" if initial else None,
                "total_count": 300 if initial else None,
                "results": references[start:end],
                "next_cursor": cursor,
                "has_more": has_more,
                "request_status": {"type": "complete"},
            }
            if initial:
                payload["expires_at"] = "2026-07-17T08:00:00.000Z"
            else:
                payload["type"] = "page"
                payload["page"] = {}
                for key in ("id", "view_id", "total_count"):
                    del payload[key]
            return payload

        paginated = [
            query_payload(100, 200, cursor="cursor-two", has_more=True),
            query_payload(200, 300, cursor=None, has_more=False),
        ]

        def opener(request, timeout):
            if request.method == "POST":
                return JsonHttpResponse(
                    query_payload(
                        0,
                        100,
                        cursor="cursor-one",
                        has_more=True,
                        initial=True,
                    )
                )
            if "/v1/pages/" not in request.full_url:
                return JsonHttpResponse(paginated.pop(0))
            page_id = request.full_url.rsplit("/", 1)[-1]
            page_calls.append(page_id)
            if page_id == "page-300":
                raise urllib.error.URLError(ssl.SSLEOFError("transient TLS EOF"))
            row = page(
                anchor="2026-07-31" if page_id == "page-001" else None,
                done=False,
                categories=(),
            )
            row["id"] = page_id
            return JsonHttpResponse(row)

        with self.assertRaisesRegex(TotalViewError, "resource limit"):
            total_from_view(
                NotionViewsApi("token-example", opener=opener, sleeper=lambda _: None),
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

        self.assertEqual(300, len(page_calls))
        self.assertEqual("page-300", page_calls[-1])

    def test_month_scan_does_not_sleep_or_retry_past_the_deadline(self):
        now = [0.0]
        requests = []
        sleeps = []

        def opener(request, timeout):
            requests.append(request)
            if request.method == "POST":
                now[0] = 179.0
                return JsonHttpResponse(
                    {
                        "object": "view_query",
                        "id": "query-example",
                        "view_id": "view-example",
                        "expires_at": "2026-07-17T08:00:00.000Z",
                        "total_count": 1,
                        "results": [{"object": "page", "id": "page-example"}],
                        "next_cursor": None,
                        "has_more": False,
                        "request_status": {"type": "complete"},
                    }
                )
            raise urllib.error.HTTPError(
                "https://example.invalid/private-page",
                429,
                "rate limited",
                {"Retry-After": "2"},
                None,
            )

        with patch("total_views.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(TotalViewError, "resource limit"):
                total_from_view(
                    NotionViewsApi(
                        "token-example",
                        opener=opener,
                        sleeper=sleeps.append,
                    ),
                    view_id="view-example",
                    target_month=date(2026, 7, 1),
                    fields=FIELDS,
                )

        self.assertEqual(["POST", "GET"], [request.method for request in requests])
        self.assertEqual([], sleeps)

    def test_month_scan_does_not_back_off_past_the_deadline(self):
        now = [0.0]
        requests = []
        sleeps = []

        def opener(request, timeout):
            requests.append(request)
            if request.method == "POST":
                now[0] = 179.9
                return JsonHttpResponse(
                    {
                        "object": "view_query",
                        "id": "query-example",
                        "view_id": "view-example",
                        "expires_at": "2026-07-17T08:00:00.000Z",
                        "total_count": 1,
                        "results": [{"object": "page", "id": "page-example"}],
                        "next_cursor": None,
                        "has_more": False,
                        "request_status": {"type": "complete"},
                    }
                )
            raise urllib.error.URLError(ssl.SSLEOFError("transient TLS EOF"))

        with patch("total_views.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(TotalViewError, "resource limit"):
                total_from_view(
                    NotionViewsApi(
                        "token-example",
                        opener=opener,
                        sleeper=sleeps.append,
                    ),
                    view_id="view-example",
                    target_month=date(2026, 7, 1),
                    fields=FIELDS,
                )

        self.assertEqual(["POST", "GET"], [request.method for request in requests])
        self.assertEqual([], sleeps)

    def test_month_scan_stops_when_pagination_exhausts_the_deadline(self):
        now = [0.0]

        class SlowPaginationApi:
            def __init__(self):
                self.calls = []

            def create_view_query(self, view_id, *, page_size):
                self.calls.append(("create", view_id, page_size))
                return {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "expires_at": "2026-07-17T08:00:00.000Z",
                    "total_count": 2,
                    "results": [{"object": "page", "id": "page-one"}],
                    "next_cursor": "cursor-one",
                    "has_more": True,
                    "request_status": {"type": "complete"},
                }

            def retrieve_page(self, page_id):
                self.calls.append(("retrieve", page_id))
                now[0] = 179.0
                row = page(anchor="2026-08-01", done=False, categories=())
                row["id"] = page_id
                return row

            def get_view_query_results(
                self,
                view_id,
                query_id,
                cursor,
                *,
                page_size,
            ):
                self.calls.append(("results", view_id, query_id, cursor, page_size))
                now[0] = 180.0
                return {
                    "object": "list",
                    "type": "page",
                    "page": {},
                    "results": [{"object": "page", "id": "page-two"}],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }

        api = SlowPaginationApi()
        with patch("total_views.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(TotalViewError, "resource limit"):
                total_from_view(
                    api,
                    view_id="view-example",
                    target_month=date(2026, 7, 1),
                    fields=FIELDS,
                )

        self.assertEqual(
            [
                ("create", "view-example", 100),
                ("retrieve", "page-one"),
                ("results", "view-example", "query-example", "cursor-one", 100),
            ],
            api.calls,
        )

    def test_month_scan_fails_before_the_301st_page_get_attempt(self):
        rows = [page(anchor="2026-08-31", done=False, categories=())]
        rows.extend(page(done=False, categories=()) for _ in range(300))
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 301,
                "results": rows[:100],
                "next_cursor": "cursor-one",
                "has_more": True,
                "request_status": {"type": "complete"},
            },
            pages=[
                {
                    "object": "list",
                    "type": "page",
                    "results": rows[100:200],
                    "next_cursor": "cursor-two",
                    "has_more": True,
                    "request_status": {"type": "complete"},
                },
                {
                    "object": "list",
                    "type": "page",
                    "results": rows[200:300],
                    "next_cursor": "cursor-three",
                    "has_more": True,
                    "request_status": {"type": "complete"},
                },
                {
                    "object": "list",
                    "type": "page",
                    "results": rows[300:],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                },
            ],
        )

        with self.assertRaisesRegex(TotalViewError, "resource limit"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

        self.assertEqual(
            300,
            sum(call[0] == "retrieve" for call in api.calls),
        )
        self.assertNotIn(
            ("results", "view-example", "query-example", "cursor-three", 100),
            api.calls,
        )

    def test_month_scan_allows_the_300th_page_get_to_confirm_the_lower_boundary(self):
        rows = [page(anchor="2026-07-31", done=False, categories=())]
        rows.extend(page(done=False, categories=()) for _ in range(298))
        rows.append(page(anchor="2026-06-30", done=False, categories=()))
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 300,
                "results": rows[:100],
                "next_cursor": "cursor-one",
                "has_more": True,
                "request_status": {"type": "complete"},
            },
            pages=[
                {
                    "object": "list",
                    "type": "page",
                    "results": rows[100:200],
                    "next_cursor": "cursor-two",
                    "has_more": True,
                    "request_status": {"type": "complete"},
                },
                {
                    "object": "list",
                    "type": "page",
                    "results": rows[200:],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                },
            ],
        )

        result = total_from_view(
            api,
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({}, result.totals)
        self.assertEqual(300, sum(call[0] == "retrieve" for call in api.calls))

    def test_month_scan_rejects_missing_initial_request_status_before_retrieve(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [page(anchor="2026-07-31")],
                "next_cursor": None,
                "has_more": False,
            }
        )

        with self.assertRaisesRegex(TotalViewError, "not complete"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

        self.assertEqual([("create", "view-example", 100)], api.calls)

    def test_month_scan_rejects_missing_paginated_status_before_that_page_retrieve(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 2,
                "results": [page(anchor="2026-08-01")],
                "next_cursor": "cursor-one",
                "has_more": True,
                "request_status": {"type": "complete"},
            },
            pages=[
                {
                    "object": "list",
                    "type": "page",
                    "results": [page(anchor="2026-07-31")],
                    "next_cursor": None,
                    "has_more": False,
                }
            ],
        )

        with self.assertRaisesRegex(TotalViewError, "not complete"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

        self.assertEqual(
            [
                ("create", "view-example", 100),
                ("retrieve", "page-example-1"),
                ("results", "view-example", "query-example", "cursor-one", 100),
            ],
            api.calls,
        )

    def test_month_scan_stops_after_first_earlier_anchor_in_one_page(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 5,
                "results": [
                    page(anchor="2026-08-01", timeboxing="not-a-block"),
                    page(anchor="2026-07-31", timeboxing="1b"),
                    page(anchor=None, timeboxing="2b"),
                    page(anchor="2026-06-30", timeboxing="99b"),
                    page(anchor="2026-07-01", timeboxing="100b"),
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

        self.assertEqual({"Research": Decimal("3")}, result.totals)
        self.assertEqual((), result.invalid_blocks)
        self.assertEqual(
            [
                ("create", "view-example", 100),
                ("retrieve", "page-example-1"),
                ("retrieve", "page-example-2"),
                ("retrieve", "page-example-3"),
                ("retrieve", "page-example-4"),
            ],
            api.calls,
        )

    def test_month_scan_rejects_a_repeated_cursor_before_hydrating_its_page(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 3,
                "results": [page(anchor="2026-08-01")],
                "next_cursor": "cursor-one",
                "has_more": True,
                "request_status": {"type": "complete"},
            },
            pages=[
                {
                    "object": "list",
                    "type": "page",
                    "results": [page(anchor="2026-06-30")],
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

        self.assertEqual(
            [
                ("create", "view-example", 100),
                ("retrieve", "page-example-1"),
                ("results", "view-example", "query-example", "cursor-one", 100),
            ],
            api.calls,
        )

    def test_month_scan_rejects_more_results_after_total_count_is_reached(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [page(anchor="2026-06-30")],
                "next_cursor": "cursor-one",
                "has_more": True,
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

        self.assertEqual([("create", "view-example", 100)], api.calls)

    def test_month_scan_keeps_target_inheritance_across_pages_and_stops_paging(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 6,
                "results": [
                    page(anchor="2026-08-01"),
                    page(anchor="2026-07-31", timeboxing="1b"),
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
                        page(anchor=None, timeboxing="2b"),
                        page(anchor="2026-06-30"),
                        page(anchor=None, timeboxing="100b"),
                    ],
                    "next_cursor": "cursor-two",
                    "has_more": True,
                    "request_status": {"type": "complete"},
                },
                {
                    "object": "list",
                    "type": "page",
                    "results": [page(anchor="2026-05-31")],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                },
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
                ("retrieve", "page-example-1"),
                ("retrieve", "page-example-2"),
                ("results", "view-example", "query-example", "cursor-one", 100),
                ("retrieve", "page-example-3"),
                ("retrieve", "page-example-4"),
            ],
            api.calls,
        )

    def test_month_scan_returns_empty_when_ordered_anchors_cross_the_target(self):
        for rows in (
            [page(anchor="2026-08-01"), page(anchor="2026-06-30")],
            [page(done=False, categories=()), page(anchor="2026-06-30")],
        ):
            with self.subTest(rows=len(rows)):
                api = FakeViewsApi(
                    {
                        "object": "view_query",
                        "id": "query-example",
                        "view_id": "view-example",
                        "total_count": len(rows),
                        "results": rows,
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

                self.assertEqual({}, result.totals)
                self.assertEqual((), result.invalid_blocks)

    def test_month_scan_accepts_a_complete_terminal_page_as_the_lower_boundary(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 2,
                "results": [
                    page(anchor="2026-07-31", timeboxing="1b"),
                    page(anchor=None, timeboxing="2b"),
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

        self.assertEqual({"Research": Decimal("3")}, result.totals)

    def test_month_scan_allows_an_empty_view_but_rejects_nonempty_without_an_anchor(self):
        empty = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 0,
                "results": [],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
        )
        no_anchor = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 1,
                "results": [page(done=False, categories=())],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
        )

        self.assertEqual(
            {},
            total_from_view(
                empty,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            ).totals,
        )
        with self.assertRaisesRegex(TotalViewError, "no structured date anchor"):
            total_from_view(
                no_anchor,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_month_scan_reports_invalid_blocks_only_inside_the_target_interval(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 3,
                "results": [
                    page(anchor="2026-08-01", timeboxing="later-invalid"),
                    page(anchor="2026-07-31", timeboxing="target-invalid"),
                    page(anchor="2026-06-30", timeboxing="earlier-invalid"),
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

        self.assertEqual({}, result.totals)
        self.assertEqual(1, len(result.invalid_blocks))
        self.assertEqual(1, result.invalid_blocks[0].row_index)
        self.assertEqual("target-invalid", result.invalid_blocks[0].value)

    def test_month_scan_preserves_invalid_block_row_index_across_newer_page_prefix(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 5,
                "results": [
                    page(anchor="2026-08-01", done=False, categories=()),
                    page(anchor=None, timeboxing="later-invalid"),
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
                        page(anchor="2026-07-31", timeboxing="target-invalid"),
                        page(anchor="2026-06-30"),
                        page(anchor="2026-07-01"),
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

        self.assertEqual(1, len(result.invalid_blocks))
        self.assertEqual(2, result.invalid_blocks[0].row_index)
        self.assertEqual("target-invalid", result.invalid_blocks[0].value)
        self.assertNotIn(("retrieve", "page-example-5"), api.calls)

    def test_month_scan_preserves_countable_item_before_first_anchor_failure(self):
        api = FakeViewsApi(
            {
                "object": "view_query",
                "id": "query-example",
                "view_id": "view-example",
                "total_count": 2,
                "results": [
                    page(anchor=None, timeboxing="1b"),
                    page(anchor="2026-06-30"),
                ],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            }
        )

        with self.assertRaisesRegex(TotalError, "before first date anchor"):
            total_from_view(
                api,
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

    def test_page_get_recovers_from_a_tls_eof(self):
        record = page(anchor="2026-07-03", done=True, timeboxing="2b")
        record["id"] = "page-example"
        outcomes = [
            JsonHttpResponse(
                {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "expires_at": "2026-07-17T08:00:00.000Z",
                    "total_count": 1,
                    "results": [{"object": "page", "id": "page-example"}],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }
            ),
            urllib.error.URLError(ssl.SSLEOFError("transient TLS EOF")),
            JsonHttpResponse(record),
        ]
        requests = []
        sleeps = []

        def opener(request, timeout):
            requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = total_from_view(
            NotionViewsApi(
                "token-example",
                opener=opener,
                sleeper=sleeps.append,
            ),
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({"Research": Decimal("2")}, result.totals)
        self.assertEqual(["POST", "GET", "GET"], [request.method for request in requests])
        self.assertEqual(1, len(sleeps))

    def test_page_get_recovers_from_connection_reset_and_timeout(self):
        for transport_error in (ConnectionResetError("reset"), TimeoutError("timeout")):
            with self.subTest(error=type(transport_error).__name__):
                record = page(anchor="2026-07-03", done=True, timeboxing="2b")
                record["id"] = "page-example"
                outcomes = [
                    JsonHttpResponse(
                        {
                            "object": "view_query",
                            "id": "query-example",
                            "view_id": "view-example",
                            "expires_at": "2026-07-17T08:00:00.000Z",
                            "total_count": 1,
                            "results": [{"object": "page", "id": "page-example"}],
                            "next_cursor": None,
                            "has_more": False,
                            "request_status": {"type": "complete"},
                        }
                    ),
                    transport_error,
                    JsonHttpResponse(record),
                ]
                requests = []
                sleeps = []

                def opener(request, timeout):
                    requests.append(request)
                    outcome = outcomes.pop(0)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                result = total_from_view(
                    NotionViewsApi(
                        "token-example",
                        opener=opener,
                        sleeper=sleeps.append,
                    ),
                    view_id="view-example",
                    target_month=date(2026, 7, 1),
                    fields=FIELDS,
                )

                self.assertEqual({"Research": Decimal("2")}, result.totals)
                self.assertEqual(["POST", "GET", "GET"], [request.method for request in requests])
                self.assertEqual(1, len(sleeps))

    def test_page_get_honors_a_bounded_retry_after_for_429(self):
        record = page(anchor="2026-07-03", done=True, timeboxing="2b")
        record["id"] = "page-example"
        outcomes = [
            JsonHttpResponse(
                {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "expires_at": "2026-07-17T08:00:00.000Z",
                    "total_count": 1,
                    "results": [{"object": "page", "id": "page-example"}],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }
            ),
            urllib.error.HTTPError(
                "https://example.invalid/private-page",
                429,
                "rate limited",
                {"Retry-After": "2"},
                None,
            ),
            JsonHttpResponse(record),
        ]
        requests = []
        sleeps = []

        def opener(request, timeout):
            requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = total_from_view(
            NotionViewsApi(
                "token-example",
                opener=opener,
                sleeper=sleeps.append,
            ),
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({"Research": Decimal("2")}, result.totals)
        self.assertEqual(["POST", "GET", "GET"], [request.method for request in requests])
        self.assertEqual([2], sleeps)

    def test_page_get_recovers_from_a_retryable_503(self):
        record = page(anchor="2026-07-03", done=True, timeboxing="2b")
        record["id"] = "page-example"
        outcomes = [
            JsonHttpResponse(
                {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "expires_at": "2026-07-17T08:00:00.000Z",
                    "total_count": 1,
                    "results": [{"object": "page", "id": "page-example"}],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }
            ),
            urllib.error.HTTPError(
                "https://example.invalid/private-page",
                503,
                "temporarily unavailable",
                {},
                None,
            ),
            JsonHttpResponse(record),
        ]
        requests = []
        sleeps = []

        def opener(request, timeout):
            requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = total_from_view(
            NotionViewsApi(
                "token-example",
                opener=opener,
                sleeper=sleeps.append,
            ),
            view_id="view-example",
            target_month=date(2026, 7, 1),
            fields=FIELDS,
        )

        self.assertEqual({"Research": Decimal("2")}, result.totals)
        self.assertEqual(["POST", "GET", "GET"], [request.method for request in requests])
        self.assertEqual(1, len(sleeps))

    def test_page_get_transient_failures_stop_after_three_sanitized_attempts(self):
        for failure_kind in ("tls", "503"):
            with self.subTest(failure_kind=failure_kind):
                def transient_error():
                    if failure_kind == "tls":
                        return urllib.error.URLError(
                            ssl.SSLEOFError("private-page-id private task content")
                        )
                    return urllib.error.HTTPError(
                        "https://example.invalid/private-page-id",
                        503,
                        "private task content",
                        {},
                        None,
                    )

                outcomes = [
                    JsonHttpResponse(
                        {
                            "object": "view_query",
                            "id": "query-example",
                            "view_id": "view-example",
                            "expires_at": "2026-07-17T08:00:00.000Z",
                            "total_count": 1,
                            "results": [{"object": "page", "id": "page-example"}],
                            "next_cursor": None,
                            "has_more": False,
                            "request_status": {"type": "complete"},
                        }
                    ),
                    transient_error(),
                    transient_error(),
                    transient_error(),
                ]
                requests = []
                sleeps = []

                def opener(request, timeout):
                    requests.append(request)
                    outcome = outcomes.pop(0)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                with self.assertRaises(TotalViewError) as caught:
                    total_from_view(
                        NotionViewsApi(
                            "private-token",
                            opener=opener,
                            sleeper=sleeps.append,
                        ),
                        view_id="view-example",
                        target_month=date(2026, 7, 1),
                        fields=FIELDS,
                    )

                self.assertEqual("page retrieve request failed", str(caught.exception))
                self.assertEqual(["POST", "GET", "GET", "GET"], [request.method for request in requests])
                self.assertEqual(2, len(sleeps))
                self.assertNotIn("private", str(caught.exception))

    def test_page_get_does_not_retry_a_non_transient_400(self):
        outcomes = [
            JsonHttpResponse(
                {
                    "object": "view_query",
                    "id": "query-example",
                    "view_id": "view-example",
                    "expires_at": "2026-07-17T08:00:00.000Z",
                    "total_count": 1,
                    "results": [{"object": "page", "id": "page-example"}],
                    "next_cursor": None,
                    "has_more": False,
                    "request_status": {"type": "complete"},
                }
            ),
            urllib.error.HTTPError(
                "https://example.invalid/private-page",
                400,
                "private request detail",
                {},
                None,
            ),
        ]
        requests = []
        sleeps = []

        def opener(request, timeout):
            requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with self.assertRaisesRegex(TotalViewError, "page retrieve request failed"):
            total_from_view(
                NotionViewsApi(
                    "private-token",
                    opener=opener,
                    sleeper=sleeps.append,
                ),
                view_id="view-example",
                target_month=date(2026, 7, 1),
                fields=FIELDS,
            )

        self.assertEqual(["POST", "GET"], [request.method for request in requests])
        self.assertEqual([], sleeps)

    def test_initial_query_post_is_never_retried(self):
        requests = []
        sleeps = []

        def opener(request, timeout):
            requests.append(request)
            raise urllib.error.URLError(ssl.SSLEOFError("transient TLS EOF"))

        with self.assertRaisesRegex(TotalViewError, "view query request failed"):
            NotionViewsApi(
                "token-example",
                opener=opener,
                sleeper=sleeps.append,
            ).create_view_query("view-example", page_size=100)

        self.assertEqual(["POST"], [request.method for request in requests])
        self.assertEqual([], sleeps)

    def test_query_results_get_is_never_retried(self):
        requests = []
        sleeps = []

        def opener(request, timeout):
            requests.append(request)
            raise urllib.error.URLError(ssl.SSLEOFError("transient TLS EOF"))

        with self.assertRaisesRegex(TotalViewError, "view query request failed"):
            NotionViewsApi(
                "token-example",
                opener=opener,
                sleeper=sleeps.append,
            ).get_view_query_results(
                "view-example",
                "query-example",
                "cursor-example",
                page_size=100,
            )

        self.assertEqual(["GET"], [request.method for request in requests])
        self.assertEqual([], sleeps)

    def test_page_get_uses_bounded_backoff_for_unsafe_retry_after(self):
        for retry_after in (
            "6",
            "-1",
            "",
            " 2 ",
            "2.5",
            "Wed, 21 Oct 2026 07:28:00 GMT",
        ):
            with self.subTest(retry_after=retry_after):
                record = page(anchor="2026-07-03", done=True, timeboxing="2b")
                record["id"] = "page-example"
                outcomes = [
                    urllib.error.HTTPError(
                        "https://example.invalid/private-page",
                        429,
                        "rate limited",
                        {"Retry-After": retry_after},
                        None,
                    ),
                    JsonHttpResponse(record),
                ]
                sleeps = []

                def opener(request, timeout):
                    outcome = outcomes.pop(0)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                response = NotionViewsApi(
                    "token-example",
                    opener=opener,
                    sleeper=sleeps.append,
                ).retrieve_page("page-example")

                self.assertEqual("page-example", response["id"])
                self.assertEqual([0.25], sleeps)

    def test_page_get_uses_bounded_backoff_for_very_long_numeric_retry_after(self):
        record = page(anchor="2026-07-03", done=True, timeboxing="2b")
        record["id"] = "page-example"
        outcomes = [
            urllib.error.HTTPError(
                "https://example.invalid/private-page",
                429,
                "rate limited",
                {"Retry-After": "9" * 5000},
                None,
            ),
            JsonHttpResponse(record),
        ]
        sleeps = []

        def opener(request, timeout):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        response = NotionViewsApi(
            "token-example",
            opener=opener,
            sleeper=sleeps.append,
        ).retrieve_page("page-example")

        self.assertEqual("page-example", response["id"])
        self.assertEqual([0.25], sleeps)

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
            "total_count": 1,
            "results": [],
            "next_cursor": "cursor-one",
            "has_more": True,
            "request_status": {"type": "complete"},
        }
        valid_page = {
            "object": "list",
            "type": "page",
            "page": {},
            "results": [page(anchor="2026-07-03")],
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
                ("retrieve", "page-example-1"),
                ("retrieve", "page-example-2"),
                ("results", "view-example", "query-example", "cursor-one", 100),
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
        with self.assertRaisesRegex(TotalViewError, "no structured date anchor"):
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
            anchor_record,
            {
                "object": "list",
                "type": "page",
                "page": {},
                "results": [{"object": "page", "id": "page-work"}],
                "next_cursor": None,
                "has_more": False,
                "request_status": {"type": "complete"},
            },
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
            "https://api.notion.com/v1/pages/page-anchor",
            requests[1].full_url,
        )
        self.assertEqual(
            "https://api.notion.com/v1/views/view-example/queries/query-example"
            "?start_cursor=cursor-one&page_size=100",
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
