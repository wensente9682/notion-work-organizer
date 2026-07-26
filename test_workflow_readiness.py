import json
from pathlib import Path
import tempfile
import unittest

from adoption_profile import FrozenInspectionEvidence, freeze_inspection_evidence
from test_adoption_profile import private_profile, ready_report
from workflow_readiness import verify_one_profile_readiness


def rich_text(value):
    return {"type": "rich_text", "rich_text": [{"plain_text": value}]}


def source_page(page_id, *, anchor=None, category="research", blocks="2b"):
    profile = private_profile()
    fields = profile["field_mapping"]
    total_fields = profile["total"]["fields"]
    properties = {
        fields["task"]: {"type": "title", "title": [{"plain_text": "Example work"}]},
        fields["done"]: {"type": "checkbox", "checkbox": True},
        fields["category"]: {
            "type": "status",
            "status": {"name": category},
        },
        fields["takeaway"]: rich_text("Example takeaway"),
        fields["improvement"]: rich_text(""),
        total_fields["timeboxing"]: rich_text(blocks),
        total_fields["date_anchor"]: {
            "type": "date",
            "date": None if anchor is None else {"start": anchor},
        },
    }
    return {
        "object": "page",
        "id": page_id,
        "properties": properties,
    }


def archive_content():
    return (
        '<details><summary>research</summary>'
        '<database url="https://example.invalid/notion/archive-reference" '
        'inline="true" data-source-url="collection://archive-source-reference">'
        "research</database></details>"
    )


class FakeTotalApi:
    def __init__(self, pages, *, fail=False, after_query=None):
        self.pages = {page["id"]: page for page in pages}
        self.fail = fail
        self.after_query = after_query
        self.calls = []

    def create_view_query(self, view_id, *, page_size):
        self.calls.append(("create_view_query",))
        if self.fail:
            raise RuntimeError("private total detail")
        response = {
            "object": "view_query",
            "id": "query-reference",
            "view_id": view_id,
            "expires_at": "2026-07-26T08:00:00+00:00",
            "total_count": len(self.pages),
            "results": [
                {"object": "page", "id": page_id} for page_id in self.pages
            ],
            "next_cursor": None,
            "has_more": False,
            "request_status": {"type": "complete"},
        }
        if self.after_query is not None:
            self.after_query()
        return response

    def retrieve_page(self, page_id):
        self.calls.append(("retrieve_page",))
        return self.pages[page_id]


class FakeOrganizeReader:
    def __init__(self, pages, *, fail=False):
        self.pages = pages
        self.fail = fail
        self.calls = []
        self.write_calls = 0

    def retrieve_database(self, reference):
        self.calls.append(("retrieve_database",))
        if self.fail:
            raise RuntimeError("private organize detail")
        profile = private_profile()
        fields = profile["field_mapping"]
        if reference == profile["source_database_id"]:
            return {
                "properties": {
                    fields["task"]: {"type": "title"},
                    fields["done"]: {"type": "checkbox"},
                    fields["category"]: {"type": "status"},
                    fields["takeaway"]: {"type": "rich_text"},
                    fields["improvement"]: {"type": "rich_text"},
                }
            }
        return {
            "properties": {
                fields["task"]: {"type": "title"},
                fields["takeaway"]: {"type": "rich_text"},
                fields["improvement"]: {"type": "rich_text"},
            }
        }

    def retrieve_archive_tables_content(self, reference):
        self.calls.append(("retrieve_archive_tables_content",))
        return archive_content()

    def query_database(self, reference, start_cursor=None, page_size=100):
        self.calls.append(("query_database",))
        return {
            "results": self.pages,
            "has_more": False,
            "next_cursor": None,
        }

    def create_archive_row(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("readiness must not write")


class WorkflowReadinessTests(unittest.TestCase):
    def test_stale_inspector_evidence_stops_before_profile_or_external_reads(self):
        profile = private_profile()
        evidence = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=90,
        )
        total_api = FakeTotalApi([])
        organize_reader = FakeOrganizeReader([])
        ignore_calls = []

        report = verify_one_profile_readiness(
            Path("/unread/private/profile.json"),
            evidence,
            "2026-07",
            total_api=total_api,
            organize_reader=organize_reader,
            ignore_checker=lambda path: ignore_calls.append(path) or True,
            now=lambda: 391,
        )

        self.assertEqual("not-ready", report.status)
        self.assertEqual([], ignore_calls)
        self.assertEqual([], total_api.calls)
        self.assertEqual([], organize_reader.calls)

    def test_future_inspector_evidence_stops_before_profile_or_external_reads(self):
        profile = private_profile()
        evidence = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=101,
        )
        total_api = FakeTotalApi([])
        organize_reader = FakeOrganizeReader([])
        ignore_calls = []

        report = verify_one_profile_readiness(
            Path("/unread/private/profile.json"),
            evidence,
            "2026-07",
            total_api=total_api,
            organize_reader=organize_reader,
            ignore_checker=lambda path: ignore_calls.append(path) or True,
            now=lambda: 100,
        )

        self.assertEqual("not-ready", report.status)
        self.assertEqual([], ignore_calls)
        self.assertEqual([], total_api.calls)
        self.assertEqual([], organize_reader.calls)

    def test_non_numeric_inspector_timestamp_fails_before_any_read(self):
        profile = private_profile()
        valid = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=90,
        )
        evidence = FrozenInspectionEvidence(
            state=valid.state,
            inspected_at="not-a-time",
            _content=valid._content,
            _digest=valid._digest,
        )
        total_api = FakeTotalApi([])
        organize_reader = FakeOrganizeReader([])
        ignore_calls = []

        report = verify_one_profile_readiness(
            Path("/unread/private/profile.json"),
            evidence,
            "2026-07",
            total_api=total_api,
            organize_reader=organize_reader,
            ignore_checker=lambda path: ignore_calls.append(path) or True,
            now=lambda: 100,
        )

        self.assertEqual("not-ready", report.status)
        self.assertEqual([], ignore_calls)
        self.assertEqual([], total_api.calls)
        self.assertEqual([], organize_reader.calls)

    def test_one_approved_profile_reaches_total_then_organize_readiness(self):
        profile = private_profile()
        pages = [
            source_page("anchor-reference", anchor="2026-07-10", blocks="1b"),
            source_page("work-reference", blocks="2b"),
        ]
        evidence = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=90,
        )
        total_api = FakeTotalApi(pages)
        organize_reader = FakeOrganizeReader(pages)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_dir = root / ".todo_archive"
            private_dir.mkdir(mode=0o700)
            profile_path = private_dir / "real_profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            profile_path.chmod(0o600)
            report = verify_one_profile_readiness(
                profile_path,
                evidence,
                "2026-07",
                total_api=total_api,
                organize_reader=organize_reader,
                ignore_checker=lambda path: True,
                now=lambda: 100,
            )

        self.assertEqual("ready", report.status)
        self.assertEqual({"research": "3"}, report.category_totals)
        self.assertEqual("3", report.grand_total)
        self.assertEqual(2, report.preview_count)
        self.assertEqual(("create_view_query",), total_api.calls[0])
        self.assertTrue(organize_reader.calls)
        self.assertEqual(0, organize_reader.write_calls)

    def test_total_failure_returns_only_sanitized_not_ready_and_skips_organize(self):
        profile = private_profile()
        evidence = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=90,
        )
        total_api = FakeTotalApi([], fail=True)
        organize_reader = FakeOrganizeReader([])

        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / ".todo_archive"
            private_dir.mkdir(mode=0o700)
            profile_path = private_dir / "real_profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            profile_path.chmod(0o600)
            report = verify_one_profile_readiness(
                profile_path,
                evidence,
                "2026-07",
                total_api=total_api,
                organize_reader=organize_reader,
                ignore_checker=lambda path: True,
                now=lambda: 100,
            )

        self.assertEqual("not-ready", report.status)
        self.assertEqual({}, report.category_totals)
        self.assertIsNone(report.grand_total)
        self.assertIsNone(report.preview_count)
        self.assertEqual([], organize_reader.calls)
        self.assertNotIn("private", report.summary)

    def test_organize_failure_discards_successful_total_instead_of_partial_readiness(self):
        profile = private_profile()
        evidence = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=90,
        )
        total_api = FakeTotalApi([])
        organize_reader = FakeOrganizeReader([], fail=True)

        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / ".todo_archive"
            private_dir.mkdir(mode=0o700)
            profile_path = private_dir / "real_profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            profile_path.chmod(0o600)
            report = verify_one_profile_readiness(
                profile_path,
                evidence,
                "2026-07",
                total_api=total_api,
                organize_reader=organize_reader,
                ignore_checker=lambda path: True,
                now=lambda: 100,
            )

        self.assertEqual("not-ready", report.status)
        self.assertEqual({}, report.category_totals)
        self.assertIsNone(report.grand_total)
        self.assertIsNone(report.preview_count)
        self.assertTrue(organize_reader.calls)
        self.assertEqual(0, organize_reader.write_calls)

    def test_stale_total_mapping_fails_before_organize_preview(self):
        profile = private_profile()
        evidence = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=90,
        )
        stale_page = source_page("stale-reference", anchor="2026-07-10")
        stale_page["properties"].pop(profile["total"]["fields"]["timeboxing"])
        total_api = FakeTotalApi([stale_page])
        organize_reader = FakeOrganizeReader([stale_page])

        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / ".todo_archive"
            private_dir.mkdir(mode=0o700)
            profile_path = private_dir / "real_profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            profile_path.chmod(0o600)
            report = verify_one_profile_readiness(
                profile_path,
                evidence,
                "2026-07",
                total_api=total_api,
                organize_reader=organize_reader,
                ignore_checker=lambda path: True,
                now=lambda: 100,
            )

        self.assertEqual("not-ready", report.status)
        self.assertEqual([], organize_reader.calls)

    def test_external_profile_change_after_total_fails_before_organize(self):
        profile = private_profile()
        evidence = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=90,
        )
        organize_reader = FakeOrganizeReader([])

        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / ".todo_archive"
            private_dir.mkdir(mode=0o700)
            profile_path = private_dir / "real_profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            profile_path.chmod(0o600)

            def change_profile():
                changed = private_profile()
                changed["source_database_id"] = "external-change-reference"
                profile_path.write_text(json.dumps(changed), encoding="utf-8")
                profile_path.chmod(0o600)

            report = verify_one_profile_readiness(
                profile_path,
                evidence,
                "2026-07",
                total_api=FakeTotalApi([], after_query=change_profile),
                organize_reader=organize_reader,
                ignore_checker=lambda path: True,
                now=lambda: 100,
            )

        self.assertEqual("not-ready", report.status)
        self.assertEqual([], organize_reader.calls)
        self.assertEqual({}, report.category_totals)

    def test_invalid_month_and_wrong_evidence_fail_before_external_reads(self):
        profile = private_profile()
        evidence = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=90,
        )
        different = private_profile()
        different["source_database_id"] = "different-reference"

        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / ".todo_archive"
            private_dir.mkdir(mode=0o700)
            profile_path = private_dir / "real_profile.json"
            profile_path.write_text(json.dumps(different), encoding="utf-8")
            profile_path.chmod(0o600)
            for month in ("2026-7", "0000-01"):
                total_api = FakeTotalApi([])
                organize_reader = FakeOrganizeReader([])
                with self.subTest(month=month):
                    report = verify_one_profile_readiness(
                        profile_path,
                        evidence,
                        month,
                        total_api=total_api,
                        organize_reader=organize_reader,
                        ignore_checker=lambda path: True,
                        now=lambda: 100,
                    )
                    self.assertEqual("not-ready", report.status)
                    self.assertEqual([], total_api.calls)
                    self.assertEqual([], organize_reader.calls)

            total_api = FakeTotalApi([])
            organize_reader = FakeOrganizeReader([])
            report = verify_one_profile_readiness(
                profile_path,
                evidence,
                "2026-07",
                total_api=total_api,
                organize_reader=organize_reader,
                ignore_checker=lambda path: True,
                now=lambda: 100,
            )
            self.assertEqual("not-ready", report.status)
            self.assertEqual([], total_api.calls)
            self.assertEqual([], organize_reader.calls)

    def test_failure_report_never_contains_private_details(self):
        profile = private_profile()
        evidence = freeze_inspection_evidence(
            ready_report(profile),
            profile,
            inspected_at=90,
        )
        private_values = (
            profile["source_database_id"],
            profile["archive_tables_page_id"],
            profile["total"]["view_id"],
            "secret-token",
            "https://private.invalid/item",
            "private task text",
        )

        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / ".todo_archive"
            private_dir.mkdir(mode=0o700)
            profile_path = private_dir / "real_profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            profile_path.chmod(0o600)
            report = verify_one_profile_readiness(
                profile_path,
                evidence,
                "2026-07",
                total_api=FakeTotalApi([], fail=True),
                organize_reader=FakeOrganizeReader([]),
                ignore_checker=lambda path: True,
                now=lambda: 100,
            )

        rendered = repr(report)
        for private in (*private_values, str(profile_path)):
            self.assertNotIn(private, rendered)


if __name__ == "__main__":
    unittest.main()
