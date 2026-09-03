import unittest
from pathlib import Path


ORGANIZE_REFERENCE = (
    Path(__file__).parent
    / "skills"
    / "todo-archive-review"
    / "references"
    / "organize.md"
)


class OrganizeConnectorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = ORGANIZE_REFERENCE.read_text(encoding="utf-8")
        cls.real_read = cls.contract.split(
            "For real-mode connector candidate reads,", 1
        )[1].split("For `organize todo-test`:", 1)[0]

    def test_uses_one_bounded_parameterized_query(self):
        self.assertEqual(1, self.real_read.count("SELECT url, createdTime"))
        expected_query = (
            'FROM "<collection_url>"\n'
            '   WHERE "<done_property>" = ?\n'
            '     AND ("<takeaway_property>" IS NOT NULL\n'
            '          OR "<improvement_property>" IS NOT NULL)\n'
            "   ORDER BY datetime(createdTime) ASC\n"
            "   LIMIT 10"
        )
        self.assertIn(expected_query, self.real_read)
        for requirement in (
            '`mode: "sql"`',
            "sole `data_source_urls` entry",
            "Run exactly one bounded query",
            "Bind the single parameter to `__YES__`",
            "`10` is the bounded read ceiling",
        ):
            self.assertIn(requirement, self.real_read)
        sql_block = self.real_read.split("```sql\n", 1)[1].split("\n   ```", 1)[0]
        for forbidden in ('"<category_property>"', "lastEditedTime", "OFFSET", "--", ";"):
            self.assertNotIn(forbidden, sql_block)
        for projected in (
            "url",
            "createdTime",
            '"<title_property>"',
            '"<done_property>"',
            '"<takeaway_property>"',
            '"<improvement_property>"',
        ):
            self.assertIn(projected, sql_block)
        self.assertIn("do not select the configured\n   category/project relation", self.real_read)
        self.assertIn("multi-data-source capability", self.real_read)
        self.assertIn("`plan_required`", self.real_read)
        self.assertIn("multiple statements", self.real_read)
        self.assertIn("alternate query fallback", self.real_read)
        self.assertNotIn('mode: "view"', self.real_read)
        self.assertNotIn("`view_url`", self.real_read)

    def test_bounded_rows_are_batch_limited_before_candidate_metadata_fetch(self):
        for requirement in (
            "remaining existing candidate eligibility rules",
            "ascending `createdTime` order",
            "configured `batch_size`",
            "Fetch only those selected candidate pages",
            "Resolve the configured category/project relation",
            "related page identities",
            "`page_last_edited_at`",
            "one unique, stable page identity/URL",
            "stop the preview on missing, duplicate, or",
            "invalid values",
            "if a selected page or required related category page cannot be resolved",
            "page identity/URL",
            "existing read-only candidate preview",
        ):
            self.assertIn(requirement, self.real_read)
        ordered_steps = (
            "Require every returned record",
            "remaining existing candidate eligibility rules",
            "ascending `createdTime` order",
            "configured `batch_size`",
            "Fetch only those selected candidate pages",
        )
        positions = [self.real_read.index(step) for step in ordered_steps]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
