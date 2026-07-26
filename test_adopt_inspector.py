import unittest

from adopt_inspector import AdoptionRequest, inspect_existing_system


SOURCE_FIELDS = {
    "task": "Work",
    "done": "Complete",
    "category": "Area",
    "takeaway": "Learning",
    "improvement": "Next time",
}
TOTAL_FIELDS = {
    "done": "Complete",
    "categories": "Area",
    "timeboxing": "Blocks",
    "date_anchor": "Date marker",
}


class FakeReader:
    def __init__(self, *, source=None, archives=None, view=None, error=None):
        self.source = source or ready_source()
        self.archives = archives or {"research": ready_archive()}
        self.view = view or ready_view()
        self.error = error
        self.calls = []

    def read_source_schema(self, reference):
        self.calls.append(("source", reference))
        if self.error == "source":
            raise RuntimeError("private page https://example.invalid/private-id")
        return self.source

    def read_archive_schema(self, reference):
        self.calls.append(("archive", reference))
        if self.error == "archive":
            raise PermissionError("archive-private-id")
        return self.archives[reference]

    def read_ordered_view(self, reference):
        self.calls.append(("view", reference))
        if self.error == "view":
            raise PermissionError("view-private-id")
        return self.view


def ready_source():
    return {
        "complete": True,
        "ambiguous": False,
        "properties": {
            "Work": {"type": "title"},
            "Complete": {"type": "checkbox"},
            "Area": {"type": "status"},
            "Learning": {"type": "rich_text"},
            "Next time": {"type": "rich_text"},
            "Blocks": {"type": "rich_text"},
            "Date marker": {"type": "date"},
        },
        "category_values": ["research"],
        "category_values_complete": True,
    }


def ready_archive():
    return {
        "complete": True,
        "ambiguous": False,
        "archive_container": "archive-container-reference",
        "properties": {
            "Work": {"type": "title"},
            "Learning": {"type": "rich_text"},
            "Next time": {"type": "rich_text"},
        },
    }


def ready_view():
    return {
        "complete": True,
        "ambiguous": False,
        "accessible": True,
        "saved_order_verified": True,
        "date_anchors_verified": True,
    }


def inspected_profile():
    return {
        "mode": "real",
        "source_database_id": "source-reference",
        "archive_tables_page_id": "archive-container-reference",
        "field_mapping": SOURCE_FIELDS,
        "archive_tables": {"research": "research"},
        "total": {
            "view_id": "ordered-view-reference",
            "fields": TOTAL_FIELDS,
        },
    }


def request(profile=None):
    profile = profile or inspected_profile()
    return AdoptionRequest(
        source=profile["source_database_id"],
        archive_targets=tuple(profile["archive_tables"].values()),
        archive_container=profile["archive_tables_page_id"],
        ordered_view=profile["total"]["view_id"],
        source_fields=profile["field_mapping"],
        total_fields=profile["total"]["fields"],
        category_routes=profile["archive_tables"],
        category_relations=profile["total"].get("category_relations", {}),
        profile=profile,
    )


class AdoptInspectorTests(unittest.TestCase):
    def test_complete_system_is_ready_through_bounded_read_only_seam(self):
        reader = FakeReader()
        report = inspect_existing_system(reader, request())
        self.assertEqual("ready", report.status)
        self.assertTrue(report.checks)
        self.assertIsInstance(report._bindings_digest, str)
        self.assertNotIn(report._bindings_digest, report.render())
        self.assertNotIn(report._bindings_digest, repr(report))
        self.assertEqual({"ready"}, {check.status for check in report.checks})
        self.assertEqual(
            [
                ("source", "source-reference"),
                ("archive", "research"),
                ("view", "ordered-view-reference"),
            ],
            reader.calls,
        )
        self.assertEqual(
            "request separate approval to generate or update the ignored private profile",
            report.next_action,
        )

    def test_partial_system_reports_missing_requirements_and_bounded_action(self):
        source = ready_source()
        source["properties"].pop("Next time")
        source["category_values"] = ["research", "unmapped"]
        report = inspect_existing_system(FakeReader(source=source), request())
        by_code = {check.code: check for check in report.checks}
        self.assertEqual("not-ready", report.status)
        self.assertEqual("missing", by_code["source.improvement"].status)
        self.assertEqual("missing", by_code["category.routing"].status)
        self.assertEqual("ready", by_code["archive.payload"].status)

    def test_incompatible_shapes_and_unverifiable_order_fail_closed(self):
        source = ready_source()
        source["properties"]["Area"] = {"type": "formula"}
        view = ready_view()
        view["saved_order_verified"] = False
        report = inspect_existing_system(FakeReader(source=source, view=view), request())
        by_code = {check.code: check for check in report.checks}
        self.assertEqual("incompatible", by_code["source.category"].status)
        self.assertEqual("incompatible", by_code["total.category"].status)
        self.assertEqual("incompatible", by_code["view.order"].status)

    def test_inaccessible_or_incomplete_reads_are_sanitized_and_fail_closed(self):
        for reader in (
            FakeReader(error="source"),
            FakeReader(view={**ready_view(), "complete": False}),
            FakeReader(source={**ready_source(), "ambiguous": True}),
        ):
            with self.subTest(reader=reader):
                report = inspect_existing_system(reader, request())
                rendered = report.render()
                self.assertEqual("not-ready", report.status)
                self.assertIn("incompatible", {check.status for check in report.checks})
                for private in (
                    "source-reference",
                    "archive-container-reference",
                    "research",
                    "archive-a",
                    "view-private-id",
                    "research",
                    "Work",
                    "Complete",
                    "Area",
                    "Learning",
                    "Next time",
                    "Blocks",
                    "Date marker",
                    "https://",
                ):
                    self.assertNotIn(private, rendered)
                self.assertNotIn("RuntimeError", rendered)
                self.assertNotIn("PermissionError", rendered)

    def test_empty_malformed_or_unmapped_categories_are_never_guessed(self):
        for values in ([""], [7], ["unknown"]):
            with self.subTest(values=values):
                source = ready_source()
                source["category_values"] = values
                report = inspect_existing_system(FakeReader(source=source), request())
                check = {item.code: item for item in report.checks}["category.routing"]
                self.assertIn(check.status, {"missing", "incompatible"})
                self.assertNotEqual("ready", report.status)

    def test_every_snapshot_requires_explicit_unambiguous_false(self):
        malformed_values = ("missing", None, "false", 0, 1)
        for scope in ("source", "archive", "view"):
            for value in malformed_values:
                with self.subTest(scope=scope, value=value):
                    source = ready_source()
                    archive = ready_archive()
                    view = ready_view()
                    snapshot = {
                        "source": source,
                        "archive": archive,
                        "view": view,
                    }[scope]
                    if value == "missing":
                        snapshot.pop("ambiguous")
                    else:
                        snapshot["ambiguous"] = value
                    report = inspect_existing_system(
                        FakeReader(
                            source=source,
                            archives={"research": archive},
                            view=view,
                        ),
                        request(),
                    )
                    self.assertEqual("not-ready", report.status)
                    self.assertEqual(
                        {"incompatible"},
                        {check.status for check in report.checks},
                    )

    def test_archive_read_exception_is_sanitized_and_fails_closed(self):
        reader = FakeReader(error="archive")

        report = inspect_existing_system(reader, request())
        rendered = report.render()

        self.assertEqual("not-ready", report.status)
        self.assertEqual({"incompatible"}, {check.status for check in report.checks})
        self.assertEqual(
            [
                ("source", "source-reference"),
                ("archive", "research"),
            ],
            reader.calls,
        )
        self.assertNotIn("archive-private-id", rendered)
        self.assertNotIn("PermissionError", rendered)


if __name__ == "__main__":
    unittest.main()
