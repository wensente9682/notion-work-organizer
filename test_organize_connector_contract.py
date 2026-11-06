import json
import subprocess
import sys
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

    def test_b19_structured_body_gate(self):
        helper = ORGANIZE_REFERENCE.parent.parent / "scripts" / "organize_body.py"
        fields = ["Name", "收获", "改进"]
        raw = {"Name": "Example", "收获": None, "改进": "1. keep <literal>"}
        rendered = {"Name": "Example", "收获": "", "改进": "<p>\n  1\\. keep <literal> \n</p>"}

        def run(row, props):
            return subprocess.run(
                [sys.executable, "-B", str(helper)],
                input=json.dumps({"fields": fields, "row": row, "properties": props}),
                text=True, capture_output=True, check=False,
            )

        expected = {"Name": "Example", "收获": "", "改进": "1. keep <literal>"}
        for props in (rendered, expected):
            result = run(raw, props)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), expected)

        # Literal tags already copied into the archive must not verify as an equal target.
        for row, props in (
            (raw, {**rendered, "改进": r"\<p><br />1. keep <literal><br />\</p>"}),
            (raw, {**rendered, "改进": "<p>\n  2. keep <literal>\n</p>"}),
            ({**raw, "改进": "2. keep <literal>"}, rendered),
            ({k: v for k, v in raw.items() if k != "收获"}, rendered),
            ({**raw, "改进": []}, rendered),
            (raw, {**rendered, "收获": None}),
        ):
            with self.subTest(row=row, props=props):
                result = run(row, props)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_b19_verified_escaped_display_is_accepted(self):
        helper = ORGANIZE_REFERENCE.parent.parent / "scripts" / "organize_body.py"
        row = {"Name": "Example", "收获": "", "改进": "1. keep <literal>"}
        rendered = {**row, "改进": r"1\. keep <literal>"}
        result = subprocess.run(
            [sys.executable, "-B", str(helper)],
            input=json.dumps({"fields": list(row), "row": row, "properties": rendered}),
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), row)

    def test_uses_one_bounded_parameterized_query(self):
        self.assertEqual(1, self.real_read.count("SELECT url, createdTime"))
        expected_query = (
            'FROM "<collection_url>"\n'
            '   WHERE "<done_property>" = ?\n'
            "   ORDER BY datetime(createdTime) ASC\n"
            "   LIMIT 10"
        )
        self.assertIn(expected_query, self.real_read)
        for requirement in (
            '`mode: "sql"`',
            "sole `data_source_urls` entry",
            "Run exactly one bounded query",
            '`data.params: ["__YES__"]`',
            "Do not send `data.parameters`",
            "`10` is the bounded read ceiling",
            "Do not filter empty takeaway/improvement rows",
            "pending-cleanup eligibility handles them",
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
        normalized_read = " ".join(self.real_read.split())
        self.assertIn("do not select the configured category/project relation", normalized_read)
        self.assertIn("multi-data-source capability", self.real_read)
        self.assertIn("`plan_required`", self.real_read)
        self.assertIn("multiple statements", self.real_read)
        self.assertIn("alternate query fallback", self.real_read)
        self.assertNotIn('mode: "view"', self.real_read)
        self.assertNotIn("`view_url`", self.real_read)

    def test_bounded_rows_resolve_category_before_eligibility_and_batch_limit(self):
        for requirement in (
            "Fetch every bounded returned candidate page",
            "Resolve the configured category/project relation",
            "pending cleanup for checked rows with empty takeaway and improvement",
            "only after Category facts are resolved",
            "remaining existing candidate eligibility rules",
            "preserve ascending `createdTime`",
            "configured `batch_size`",
            "related page identities",
            "`page_last_edited_at`",
            "one unique, stable page identity/URL",
            "stop the preview on missing, duplicate, or",
            "invalid values",
            "if a candidate page or required related category page cannot be resolved",
            "page identity/URL",
            "existing read-only candidate preview",
        ):
            self.assertIn(requirement, self.real_read)
        ordered_steps = (
            "Require every returned record",
            "Fetch every bounded returned candidate page",
            "Resolve the configured category/project relation",
            "remaining existing candidate eligibility rules",
            "preserve ascending `createdTime`",
            "configured `batch_size`",
        )
        positions = [self.real_read.index(step) for step in ordered_steps]
        self.assertEqual(positions, sorted(positions))

    def test_ok_requires_persisted_content_fingerprint_before_any_write(self):
        for requirement in (
            "exact `content_fingerprint` key",
            "snapshot as incomplete",
            "refuse `ok` before any Notion write",
            "Require the approved candidate's saved `content_fingerprint`",
            "If the fingerprint is missing, refuse the operation before any Notion write",
        ):
            self.assertIn(requirement, self.contract)

    def test_confirm_cleanup_handles_sandboxed_keychain_access_explicitly(self):
        for requirement in (
            "must not be treated as evidence that the credential is absent",
            "Only after `done` -> explicit `confirm`",
            "connector has re-read and bilaterally verified",
            "single user-approved sandbox-escalated execution",
            "existing Keychain credential and the public Notion API",
            "Keep the token in process memory only; never print, persist, relay, or copy it",
            "Do not automatically retry",
            "Do not use this escalation for preview, `ok`, manual-match, archive row creation, `dismiss`, `skip`, `undo`, `done`, or earlier verification",
        ):
            self.assertIn(requirement, self.contract)


if __name__ == "__main__":
    unittest.main()
