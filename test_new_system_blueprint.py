import unittest

from new_system_blueprint import canonical_blueprint, validate_canonical_blueprint


class NewSystemBlueprintTests(unittest.TestCase):
    def canonical(self):
        return canonical_blueprint(
            archive_container_id="archive-container",
            archive_databases={"Research": "archive-research"},
        )

    def test_canonical_blank_fixture_is_ready(self):
        blueprint = self.canonical()

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("ready", report.status)
        self.assertEqual((), report.issues)
        self.assertEqual("select", blueprint["source"]["properties"]["Category"])
        self.assertEqual("date", blueprint["source"]["properties"]["Work Date"])
        self.assertEqual("rich_text", blueprint["source"]["properties"]["Time Blocks"])
        self.assertEqual(
            {"property": "Work Date", "direction": "descending"},
            blueprint["view"]["sort"],
        )
        self.assertEqual(
            {
                "id": "archive-research",
                "parent_id": "archive-container",
                "category": "Research",
                "properties": {
                    "Task": "title",
                    "Takeaway": "rich_text",
                    "Improvement": "rich_text",
                },
            },
            blueprint["archives"]["Research"],
        )

    def test_wrong_category_type_is_structured_not_ready(self):
        blueprint = self.canonical()
        blueprint["source"]["properties"]["Category"] = "status"

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("source.property-type", report.issues[0].code)
        self.assertEqual("source.properties.Category", report.issues[0].path)
        self.assertEqual("select", report.issues[0].expected)
        self.assertEqual("status", report.issues[0].actual)

    def test_wrong_work_date_type_is_structured_not_ready(self):
        blueprint = self.canonical()
        blueprint["source"]["properties"]["Work Date"] = "rich_text"

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("source.property-type", report.issues[0].code)
        self.assertEqual("source.properties.Work Date", report.issues[0].path)
        self.assertEqual("date", report.issues[0].expected)
        self.assertEqual("rich_text", report.issues[0].actual)

    def test_wrong_time_blocks_type_is_structured_not_ready(self):
        blueprint = self.canonical()
        blueprint["source"]["properties"]["Time Blocks"] = "number"

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("source.property-type", report.issues[0].code)
        self.assertEqual("source.properties.Time Blocks", report.issues[0].path)
        self.assertEqual("rich_text", report.issues[0].expected)
        self.assertEqual("number", report.issues[0].actual)

    def test_wrong_source_schema_type_is_structured_not_ready(self):
        blueprint = self.canonical()
        blueprint["source"]["properties"]["Task"] = "rich_text"

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("source.property-type", report.issues[0].code)
        self.assertEqual("source.properties.Task", report.issues[0].path)
        self.assertEqual("title", report.issues[0].expected)
        self.assertEqual("rich_text", report.issues[0].actual)

    def test_wrong_view_sort_is_structured_not_ready(self):
        cases = (
            ("property", "Created", "view.sort.property", "Work Date"),
            ("direction", "ascending", "view.sort.direction", "descending"),
        )
        for key, value, path, expected in cases:
            with self.subTest(key=key):
                blueprint = self.canonical()
                blueprint["view"]["sort"][key] = value

                report = validate_canonical_blueprint(blueprint)

                self.assertEqual("not-ready", report.status)
                self.assertEqual("view.sort", report.issues[0].code)
                self.assertEqual(path, report.issues[0].path)
                self.assertEqual(expected, report.issues[0].expected)
                self.assertEqual(value, report.issues[0].actual)

    def test_unconfigured_view_is_structured_not_ready(self):
        blueprint = self.canonical()
        blueprint["view"]["configured"] = False

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("view.configured", report.issues[0].code)
        self.assertEqual("view.configured", report.issues[0].path)
        self.assertIs(report.issues[0].expected, True)
        self.assertIs(report.issues[0].actual, False)

    def test_wrong_archive_parent_is_structured_not_ready(self):
        blueprint = self.canonical()
        blueprint["archives"]["Research"]["parent_id"] = "other-container"

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("archive.parent", report.issues[0].code)
        self.assertEqual("archives.Research.parent_id", report.issues[0].path)
        self.assertEqual("archive-container", report.issues[0].expected)
        self.assertEqual("other-container", report.issues[0].actual)

    def test_unapproved_archive_container_is_structured_not_ready(self):
        blueprint = self.canonical()
        blueprint["archive_container"]["approved"] = False

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("archive-container.approved", report.issues[0].code)
        self.assertEqual("archive_container.approved", report.issues[0].path)
        self.assertIs(report.issues[0].expected, True)
        self.assertIs(report.issues[0].actual, False)

    def test_wrong_archive_category_is_structured_not_ready(self):
        blueprint = self.canonical()
        blueprint["archives"]["Research"]["category"] = "Writing"

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("archive.category", report.issues[0].code)
        self.assertEqual("archives.Research.category", report.issues[0].path)
        self.assertEqual("Research", report.issues[0].expected)
        self.assertEqual("Writing", report.issues[0].actual)

    def test_wrong_archive_schema_is_structured_not_ready(self):
        blueprint = self.canonical()
        blueprint["archives"]["Research"]["properties"]["Takeaway"] = "number"

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("archive.schema", report.issues[0].code)
        self.assertEqual(
            "archives.Research.properties.Takeaway",
            report.issues[0].path,
        )
        self.assertEqual("rich_text", report.issues[0].expected)
        self.assertEqual("number", report.issues[0].actual)

    def test_each_declared_category_requires_one_archive(self):
        blueprint = canonical_blueprint(
            archive_container_id="archive-container",
            archive_databases={
                "Research": "archive-research",
                "Writing": "archive-writing",
            },
        )
        self.assertEqual(
            ("Research", "Writing"),
            blueprint["source"]["category_options"],
        )
        del blueprint["archives"]["Writing"]

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("archive.missing", report.issues[0].code)
        self.assertEqual("archives.Writing", report.issues[0].path)
        self.assertEqual("direct-child archive database", report.issues[0].expected)
        self.assertIsNone(report.issues[0].actual)

    def test_archive_container_id_must_be_non_empty(self):
        for value in ("", None):
            with self.subTest(value=value):
                blueprint = self.canonical()
                if value is None:
                    del blueprint["archive_container"]["id"]
                else:
                    blueprint["archive_container"]["id"] = value

                report = validate_canonical_blueprint(blueprint)

                issue = next(
                    item
                    for item in report.issues
                    if item.path == "archive_container.id"
                )
                self.assertEqual("not-ready", report.status)
                self.assertEqual("archive-container.id", issue.code)
                self.assertEqual("non-empty string", issue.expected)
                self.assertEqual(value, issue.actual)

    def test_archive_database_id_must_be_non_empty(self):
        for value in ("", None):
            with self.subTest(value=value):
                blueprint = self.canonical()
                if value is None:
                    del blueprint["archives"]["Research"]["id"]
                else:
                    blueprint["archives"]["Research"]["id"] = value

                report = validate_canonical_blueprint(blueprint)

                issue = next(
                    item
                    for item in report.issues
                    if item.path == "archives.Research.id"
                )
                self.assertEqual("not-ready", report.status)
                self.assertEqual("archive.id", issue.code)
                self.assertEqual("non-empty string", issue.expected)
                self.assertEqual(value, issue.actual)

    def test_category_name_must_be_non_empty(self):
        blueprint = canonical_blueprint(
            archive_container_id="archive-container",
            archive_databases={"": "archive-empty"},
        )

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("source.category", report.issues[0].code)
        self.assertEqual("source.category_options[0]", report.issues[0].path)
        self.assertEqual("non-empty string", report.issues[0].expected)
        self.assertEqual("", report.issues[0].actual)

    def test_category_options_must_be_present_and_a_sequence(self):
        cases = (None, "Research", ())
        for value in cases:
            with self.subTest(value=value):
                blueprint = self.canonical()
                if value is None:
                    del blueprint["source"]["category_options"]
                else:
                    blueprint["source"]["category_options"] = value

                report = validate_canonical_blueprint(blueprint)

                issue = next(
                    item
                    for item in report.issues
                    if item.path == "source.category_options"
                )
                self.assertEqual("not-ready", report.status)
                self.assertEqual("source.category-options", issue.code)
                self.assertEqual("non-empty sequence", issue.expected)
                self.assertEqual(value, issue.actual)

    def test_archive_database_ids_must_be_unique(self):
        blueprint = canonical_blueprint(
            archive_container_id="archive-container",
            archive_databases={
                "Research": "archive-shared",
                "Writing": "archive-shared",
            },
        )

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("archive.id-unique", report.issues[0].code)
        self.assertEqual("archives.Writing.id", report.issues[0].path)
        self.assertEqual("unique archive database ID", report.issues[0].expected)
        self.assertEqual("archive-shared", report.issues[0].actual)

    def test_missing_container_and_parent_ids_do_not_match(self):
        blueprint = self.canonical()
        del blueprint["archive_container"]["id"]
        del blueprint["archives"]["Research"]["parent_id"]

        report = validate_canonical_blueprint(blueprint)

        issues = {item.path: item for item in report.issues}
        self.assertEqual("not-ready", report.status)
        self.assertEqual(
            "archive-container.id",
            issues["archive_container.id"].code,
        )
        self.assertEqual("archive.parent", issues["archives.Research.parent_id"].code)
        self.assertEqual(
            "non-empty archive container ID",
            issues["archives.Research.parent_id"].expected,
        )
        self.assertIsNone(issues["archives.Research.parent_id"].actual)

    def test_category_options_must_not_repeat_a_category(self):
        blueprint = self.canonical()
        blueprint["source"]["category_options"] = ("Research", "Research")

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("source.category-unique", report.issues[0].code)
        self.assertEqual("source.category_options[1]", report.issues[0].path)
        self.assertEqual("unique category", report.issues[0].expected)
        self.assertEqual("Research", report.issues[0].actual)

    def test_archive_category_must_be_declared(self):
        blueprint = self.canonical()
        blueprint["archives"]["Writing"] = {
            "id": "archive-writing",
            "parent_id": "archive-container",
            "category": "Writing",
            "properties": {
                "Task": "title",
                "Takeaway": "rich_text",
                "Improvement": "rich_text",
            },
        }

        report = validate_canonical_blueprint(blueprint)

        self.assertEqual("not-ready", report.status)
        self.assertEqual("archive.unexpected", report.issues[0].code)
        self.assertEqual("archives.Writing", report.issues[0].path)
        self.assertEqual("declared category", report.issues[0].expected)
        self.assertEqual("Writing", report.issues[0].actual)


if __name__ == "__main__":
    unittest.main()
