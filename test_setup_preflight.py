import json
import tempfile
import unittest
from pathlib import Path

import setup_preflight


def write_json(directory, name, data):
    path = Path(directory) / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def complete_config():
    return {
        "source_database_id": "source-db",
        "source_order": "bottom_first",
        "batch_size": 5,
        "move_limit": 30,
        "target_databases": {
            "example-category": "archive-db",
        },
    }


def complete_schema():
    return {
        "source": {
            "properties": {
                "Name": {"type": "title"},
                "完成": {"type": "checkbox"},
                "category": {"type": "select"},
                "收获": {"type": "rich_text"},
                "改进": {"type": "rich_text"},
            }
        },
        "archives": {
            "example-category": {
                "properties": {
                    "Name": {"type": "title"},
                    "收获": {"type": "rich_text"},
                    "改进": {"type": "rich_text"},
                }
            }
        },
    }


def english_config():
    cfg = complete_config()
    cfg["field_mapping"] = {
        "task": "Task",
        "done": "Done",
        "category": "Category",
        "takeaway": "Takeaway",
        "improvement": "Improvement",
    }
    return cfg


def english_schema():
    return {
        "source": {
            "properties": {
                "Task": {"type": "title"},
                "Done": {"type": "checkbox"},
                "Category": {"type": "select"},
                "Takeaway": {"type": "rich_text"},
                "Improvement": {"type": "rich_text"},
            }
        },
        "archives": {
            "example-category": {
                "properties": {
                    "Task": {"type": "title"},
                    "Takeaway": {"type": "rich_text"},
                    "Improvement": {"type": "rich_text"},
                }
            }
        },
    }


class SetupPreflightTest(unittest.TestCase):
    def test_placeholder_config_reports_warnings(self):
        findings = setup_preflight.validate_config(
            {
                "source_database_id": "YOUR_TODO_SOURCE_DATABASE_ID",
                "source_order": "bottom_first",
                "batch_size": 5,
                "move_limit": 30,
                "target_databases": {
                    "example-category": "YOUR_EXAMPLE_CATEGORY_ARCHIVE_DATABASE_ID",
                },
            }
        )

        self.assertFalse(any(finding.level == "error" for finding in findings))
        self.assertTrue(any(finding.code == "config.placeholder" for finding in findings))

    def test_complete_fake_config_and_schema_pass(self):
        findings = setup_preflight.validate_config(complete_config())
        findings.extend(setup_preflight.validate_schema(complete_schema()))

        self.assertEqual([], findings)

    def test_english_field_mapping_schema_passes(self):
        cfg = english_config()
        findings = setup_preflight.validate_config(cfg)
        findings.extend(setup_preflight.validate_schema(english_schema(), cfg))

        self.assertEqual([], findings)

    def test_missing_fields_fake_schema_reports_errors(self):
        schema = complete_schema()
        del schema["source"]["properties"]["收获"]
        del schema["archives"]["example-category"]["properties"]["改进"]

        findings = setup_preflight.validate_schema(schema)

        messages = [finding.message for finding in findings]
        self.assertIn("source is missing required property: 收获", messages)
        self.assertIn("archive[example-category] is missing required property: 改进", messages)

    def test_cli_run_returns_nonzero_for_missing_schema_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_json(directory, "config.json", complete_config())
            schema = complete_schema()
            del schema["source"]["properties"]["category"]
            schema_path = write_json(directory, "schema.json", schema)

            report, exit_code = setup_preflight.run(config_path, schema_path)

        self.assertEqual(1, exit_code)
        self.assertIn("source is missing required property: category", report)
        self.assertIn("Not performed", report)


if __name__ == "__main__":
    unittest.main()
