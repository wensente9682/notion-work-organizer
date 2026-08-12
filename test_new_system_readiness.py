import json
import unittest

from new_system_readiness import verify_new_system_readiness


def profile_content():
    return json.dumps({
        "mode": "new-system-v0.3",
        "source_database_id": "source-db",
        "archive_tables_page_id": "target-page",
        "archive_tables": {"Study": "study-db", "Personal": "personal-db"},
        "source": {
            "id": "source-db",
            "properties": {
                "Task": "title", "Done": "checkbox", "Category": "select",
                "Takeaway": "rich_text", "Improvement": "rich_text",
                "Work Date": "date", "Time Blocks": "rich_text",
            },
            "categories": ["Study", "Personal"],
        },
        "view": {
            "id": "source-ds",
            "configured": True,
            "sort": {"property": "Work Date", "direction": "descending"},
        },
        "archive_container": {"id": "target-page", "approved": True},
        "archives": {
            "Study": {
                "id": "study-db", "parent_id": "target-page", "category": "Study",
                "properties": {"Task": "title", "Takeaway": "rich_text", "Improvement": "rich_text"},
            },
            "Personal": {
                "id": "personal-db", "parent_id": "target-page", "category": "Personal",
                "properties": {"Task": "title", "Takeaway": "rich_text", "Improvement": "rich_text"},
            },
        },
    }, sort_keys=True).encode()


class NewSystemReadinessTests(unittest.TestCase):
    def test_empty_verified_system_runs_total_then_enters_organize_preview(self):
        report = verify_new_system_readiness(
            profile_content(),
            source_schema={
                "Task": "title", "Done": "checkbox", "Category": "select",
                "Takeaway": "rich_text", "Improvement": "rich_text",
                "Work Date": "date", "Time Blocks": "rich_text",
            },
            archive_schemas={
                "Study": {"Task": "title", "Takeaway": "rich_text", "Improvement": "rich_text"},
                "Personal": {"Task": "title", "Takeaway": "rich_text", "Improvement": "rich_text"},
            },
            source_rows=[],
            target_month="2026-08",
        )

        self.assertEqual("ready", report.status)
        self.assertEqual({}, report.category_totals)
        self.assertEqual("0", report.grand_total)
        self.assertEqual(0, report.preview_count)
        self.assertEqual(("total", "organize-preview"), report.phases)

    def test_required_schemas_allow_unrelated_extra_properties(self):
        report = verify_new_system_readiness(
            profile_content(),
            source_schema={
                "Task": "title", "Done": "checkbox", "Category": "select",
                "Takeaway": "rich_text", "Improvement": "rich_text",
                "Work Date": "date", "Time Blocks": "rich_text",
                "Custom Source Note": "rich_text",
            },
            archive_schemas={
                "Study": {
                    "Task": "title", "Takeaway": "rich_text", "Improvement": "rich_text",
                    "Custom Archive Note": "rich_text",
                },
                "Personal": {
                    "Task": "title", "Takeaway": "rich_text", "Improvement": "rich_text",
                    "Custom Archive Flag": "checkbox",
                },
            },
            source_rows=[],
            target_month="2026-08",
        )

        self.assertEqual("ready", report.status)

    def test_schema_or_unbounded_connector_facts_fail_closed(self):
        arguments = {
            "source_schema": {
                "Task": "title", "Done": "checkbox", "Category": "select",
                "Takeaway": "rich_text", "Improvement": "rich_text",
                "Work Date": "date", "Time Blocks": "rich_text",
            },
            "archive_schemas": {
                "Study": {"Task": "title", "Takeaway": "rich_text", "Improvement": "rich_text"},
                "Personal": {"Task": "title", "Takeaway": "rich_text", "Improvement": "rich_text"},
            },
            "source_rows": [],
            "target_month": "2026-08",
        }
        for change in (
            {"source_schema": {**arguments["source_schema"], "Done": "text"}},
            {"source_schema": {
                name: property_type
                for name, property_type in arguments["source_schema"].items()
                if name != "Work Date"
            }},
            {"archive_schemas": {"Study": arguments["archive_schemas"]["Study"]}},
            {"archive_schemas": {
                **arguments["archive_schemas"],
                "Study": {"Task": "title", "Takeaway": "rich_text"},
            }},
            {"source_rows": [{}] * 101},
        ):
            with self.subTest(change=change):
                report = verify_new_system_readiness(profile_content(), **{**arguments, **change})
                self.assertEqual("not-ready", report.status)


if __name__ == "__main__":
    unittest.main()
