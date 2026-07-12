import argparse
import contextlib
import io
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import notion_todo_workflow as workflow


SOURCE_DB = "SRC_DB"
TARGET_DB = "TGT_DB"
LEDGER_DB = "LEDGER_DB"
SOURCE_ID = "SRC_PAGE"
TARGET_ID = "TGT_PAGE"
RESEARCH_ARCHIVE_DB = "ARCH_RESEARCH"
WRITING_ARCHIVE_DB = "ARCH_WRITING"
ADMIN_ARCHIVE_DB = "ARCH_ADMIN"
RESEARCH_PROJECT_ID = "PROJ_RESEARCH"
WRITING_PROJECT_ID = "PROJ_WRITING"
ADMIN_PROJECT_ID = "PROJ_ADMIN"
ARCHIVE_TABLES_PAGE_ID = "ARCHIVE_TABLES"


def rich_text(value):
    return {"type": "rich_text", "rich_text": [{"plain_text": str(value)}]}


def select(value):
    return {"type": "select", "select": {"name": value}}


def url(value):
    return {"type": "url", "url": value}


def number(value):
    return {"type": "number", "number": value}


def relation(*page_ids):
    return {"type": "relation", "relation": [{"id": page_id} for page_id in page_ids]}


def ledger_page(*, operation_id="op-1", source_id=SOURCE_ID, target_id=TARGET_ID, action="moved"):
    return {
        "id": "ledger-page-id",
        "url": "https://example.invalid/notion/ledger-page-id",
        "properties": {
            "action": select(action),
            "session_id": rich_text("session-1"),
            "operation_id": rich_text(operation_id),
            "source": url(f"https://example.invalid/notion/{source_id}"),
            "target": url(f"https://example.invalid/notion/{target_id}"),
            "category": rich_text("alpha"),
            "batch": rich_text("1"),
            "number": number(1),
        },
    }


def page(page_id, database_id):
    props = {
        "完成": {"type": "checkbox", "checkbox": True},
        "Name": {"type": "title", "title": [{"plain_text": "moved item"}]},
        "收获": rich_text(""),
        "改进": rich_text(""),
    }
    if database_id == SOURCE_DB:
        props["category"] = rich_text("alpha")
    return {
        "id": page_id,
        "archived": False,
        "created_time": "2026-07-01T00:00:00Z",
        "last_edited_time": "2026-07-02T03:04:05Z",
        "parent": {"type": "database_id", "database_id": database_id},
        "properties": props,
    }


def source_row(page_id, *, name, category="alpha", done=True, learnings="", improvements=""):
    return {
        "id": page_id,
        "url": f"https://example.invalid/notion/{page_id}",
        "created_time": "2026-07-01T00:00:00Z",
        "last_edited_time": "2026-07-02T03:04:05Z",
        "archived": False,
        "parent": {"type": "database_id", "database_id": SOURCE_DB},
        "properties": {
            "完成": {"type": "checkbox", "checkbox": done},
            "Name": {"type": "title", "title": [{"plain_text": name}]},
            "category": rich_text(category),
            "收获": rich_text(learnings),
            "改进": rich_text(improvements),
        },
    }


def real_source_row(page_id, *, name, categories, done=True, learnings="", improvements=""):
    row = source_row(page_id, name=name, done=done, learnings=learnings, improvements=improvements)
    row["properties"]["category"] = relation(*categories)
    return row


def archive_row(page_id, *, name, learnings="", improvements=""):
    return {
        "id": page_id,
        "url": f"https://example.invalid/notion/{page_id}",
        "created_time": "2026-07-01T00:00:00Z",
        "archived": False,
        "parent": {"type": "database_id", "database_id": TARGET_DB},
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": name}]},
            "收获": rich_text(learnings),
            "改进": rich_text(improvements),
        },
    }


def english_config():
    cfg = config()
    cfg["field_mapping"] = {
        "task": "Task",
        "done": "Done",
        "category": "Category",
        "takeaway": "Takeaway",
        "improvement": "Improvement",
    }
    return cfg


def english_source_row(page_id, *, name, category="example-category", done=True, takeaway="", improvement=""):
    return {
        "id": page_id,
        "url": f"https://example.invalid/notion/{page_id}",
        "created_time": "2026-07-01T00:00:00Z",
        "archived": False,
        "parent": {"type": "database_id", "database_id": SOURCE_DB},
        "properties": {
            "Done": {"type": "checkbox", "checkbox": done},
            "Task": {"type": "title", "title": [{"plain_text": name}]},
            "Category": rich_text(category),
            "Takeaway": rich_text(takeaway),
            "Improvement": rich_text(improvement),
        },
    }


def real_archive_row(page_id, *, name, categories, learnings="", improvements=""):
    row = archive_row(page_id, name=name, learnings=learnings, improvements=improvements)
    row["properties"]["项目清单"] = relation(*categories)
    return row


class FakeClient:
    def __init__(self, ledger_results):
        self.ledger_results = ledger_results
        self.cache_path = Path(tempfile.gettempdir()) / f"todo-test-fake-cache-{uuid.uuid4().hex}.json"
        self.archive_calls = []
        self.created_archive_rows = []
        self.safety_log_rows = []
        self.ledger_updates = []
        self.undo_rows = []

    def query_database(self, database_id, start_cursor=None, page_size=100):
        self.queried_database_id = database_id
        self.queried_page_size = page_size
        return {"results": self.ledger_results, "has_more": False}

    def retrieve_page(self, page_id):
        compact = workflow.notion_id(page_id)
        if compact == SOURCE_ID:
            return page(SOURCE_ID, SOURCE_DB)
        if compact == TARGET_ID:
            return page(TARGET_ID, TARGET_DB)
        if compact.startswith(RESEARCH_ARCHIVE_DB):
            return page(compact, RESEARCH_ARCHIVE_DB)
        if compact.startswith(WRITING_ARCHIVE_DB):
            return page(compact, WRITING_ARCHIVE_DB)
        return page(compact, "unexpected-database")

    def archive_page(self, page_id):
        self.archive_calls.append(page_id)
        return {"id": page_id}

    def create_archive_row(self, database_id, candidate, **kwargs):
        self.created_archive_rows.append((database_id, candidate, kwargs))
        return {"id": TARGET_ID, "url": f"https://example.invalid/notion/{TARGET_ID}"}

    def retrieve_archive_tables_content(self, page_id):
        return getattr(self, "archive_tables_content", "")

    def update_archive_tables_content(self, page_id, content):
        self.archive_tables_page_id = page_id
        self.archive_tables_content = content
        self.archive_tables_updates = getattr(self, "archive_tables_updates", [])
        self.archive_tables_updates.append((page_id, content))
        return {"id": page_id}

    def create_safety_log_page(self, parent_page_id, **kwargs):
        self.safety_log_rows.append((parent_page_id, kwargs))
        return {"id": "safety-log-page-id", "url": "https://example.invalid/notion/safety-log-page-id"}

    def create_undo_ledger_row(self, database_id, **kwargs):
        self.undo_rows.append((database_id, kwargs))
        return {"id": "undo-ledger-page-id", "url": "https://example.invalid/notion/undo-ledger-page-id"}

    def create_manual_match_ledger_row(self, database_id, **kwargs):
        return {"id": "manual-ledger-page-id", "url": "https://example.invalid/notion/manual-ledger-page-id"}

    def update_ledger_stability(self, page_id, stability):
        self.ledger_updates.append((page_id, stability))
        return {"id": page_id}


def config():
    return {
        "source_database_id": SOURCE_DB,
        "ledger_database_id": LEDGER_DB,
        "target_databases": {"alpha": TARGET_DB},
    }


def real_config():
    return {
        "mode": "real",
        "source_database_id": SOURCE_DB,
        "archive_tables_page_id": ARCHIVE_TABLES_PAGE_ID,
        "archive_tables": {
            "research": "research",
            "writing": "writing",
            "admin": "admin",
        },
        "project_categories": {
            "research": RESEARCH_PROJECT_ID,
            "writing": WRITING_PROJECT_ID,
            "admin": ADMIN_PROJECT_ID,
        },
    }


def moved_record(target_id=TARGET_ID):
    return {
        "session_id": "session-1",
        "operation_id": "op-1",
        "batch_id": 1,
        "batch_number": 1,
        "source_page_id": SOURCE_ID,
        "source_url": f"https://example.invalid/notion/{SOURCE_ID}",
        "target_page_id": target_id,
        "target_url": f"https://example.invalid/notion/{target_id}",
        "category": "alpha",
        "name": "moved item",
        "ledger_page_id": "ledger-page-id",
    }


def dismissed_record(page_id=SOURCE_ID):
    candidate = workflow.Candidate.from_notion_page(page(page_id, SOURCE_DB))
    return {
        "action": "dismissed",
        "session_id": "session-1",
        "operation_id": "dismiss-op-1",
        "batch_id": 1,
        "batch_number": 1,
        "source_page_id": page_id,
        "source_url": f"https://example.invalid/notion/{page_id}",
        "category": candidate.category,
        "name": candidate.name,
        "fingerprint": workflow.candidate_fingerprint(candidate),
    }


def archive_tables_content():
    return f"""<details>
<summary>research</summary>
\t<database url="https://example.invalid/notion/{RESEARCH_ARCHIVE_DB}" inline="true" data-source-url="collection://FAKE_RESEARCH_DATA_SOURCE_ID">research</database>
</details>
<details>
<summary>writing</summary>
\t<database url="https://example.invalid/notion/{WRITING_ARCHIVE_DB}" inline="true" data-source-url="collection://FAKE_WRITING_DATA_SOURCE_ID">writing</database>
</details>
<details>
<summary>admin</summary>
\t<database url="https://example.invalid/notion/{ADMIN_ARCHIVE_DB}" inline="true" data-source-url="collection://FAKE_ADMIN_DATA_SOURCE_ID">admin</database>
</details>
"""


class SessionOptionsTest(unittest.TestCase):
    def test_options_default_from_config(self):
        cfg = {"batch_size": 8, "move_limit": 40}
        args = argparse.Namespace(batch_size=None, move_limit=None)

        self.assertEqual(workflow.organize_options(cfg, args), {"batch_size": 8, "move_limit": 40})

    def test_options_override_config_and_persist_to_state(self):
        cfg = {"batch_size": 5, "move_limit": 30}
        args = argparse.Namespace(batch_size=9, move_limit=41)
        state = {}

        self.assertEqual(workflow.session_options(state, cfg, args), {"batch_size": 9, "move_limit": 41})
        self.assertEqual(state["session_options"], {"batch_size": 9, "move_limit": 41})


class FieldMappingTest(unittest.TestCase):
    def test_english_field_mapping_reads_candidates_and_writes_archive_rows(self):
        cfg = english_config()
        source = english_source_row(
            SOURCE_ID,
            name="mapped task",
            category="example-category",
            takeaway="learned",
            improvement="next time",
        )

        candidate = workflow.Candidate.from_notion_page(source, cfg)

        self.assertEqual(candidate.name, "mapped task")
        self.assertEqual(candidate.category, "example-category")
        self.assertEqual(candidate.learnings, "learned")
        self.assertEqual(candidate.improvements, "next time")
        self.assertEqual(workflow.eligible_candidate(source, cfg), candidate)

        client = workflow.NotionClient("token")
        with patch.object(client, "request", return_value={"id": TARGET_ID, "url": f"https://example.invalid/notion/{TARGET_ID}"}) as request:
            client.create_archive_row(TARGET_DB, candidate, cfg)

        body = request.call_args.args[2]
        props = body["properties"]
        self.assertIn("Task", props)
        self.assertIn("Takeaway", props)
        self.assertIn("Improvement", props)
        self.assertNotIn("Name", props)
        self.assertEqual(props["Task"]["title"][0]["text"]["content"], "mapped task")



class AutoContinueActiveBatchTest(unittest.TestCase):
    def test_next_reuses_active_batch_without_config_or_notion(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            workflow.save_json(
                state_path,
                {
                    "batch_id": 7,
                    "batch": [
                        {
                            "source_page_id": "abc123",
                            "source_url": "https://example.invalid/notion/abc123",
                            "name": "sample task",
                            "category": "alpha",
                            "learnings": "learned something",
                            "improvements": "",
                            "created_time": "2026-07-01T00:00:00Z",
                            "last_edited_time": "2026-07-03T04:05:06Z",
                            "fingerprint": "test",
                        }
                    ],
                    "moved": [],
                    "skipped": [],
                },
            )
            args = argparse.Namespace(config=tmp_path / "missing-config.json", state=state_path, limit=5, force=False)
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                workflow.command_next(args)

            rendered = output.getvalue()
            self.assertIn("Continuing previous organize batch.", rendered)
            self.assertIn("sample task", rendered)
            self.assertIn("added: 2026-07-01 00:00", rendered)
            self.assertIn("last edited: 2026-07-03 04:05", rendered)
            self.assertIn("ok 1 3", rendered)
            self.assertIn("dismiss 1", rendered)

    def test_next_resume_uses_configured_field_labels_when_config_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            workflow.save_json(config_path, english_config())
            workflow.save_json(
                state_path,
                {
                    "batch_id": 7,
                    "batch": [
                        {
                            "source_page_id": "abc123",
                            "source_url": "https://example.invalid/notion/abc123",
                            "name": "sample task",
                            "category": "example-category",
                            "learnings": "learned something",
                            "improvements": "try another way",
                            "created_time": "2026-07-01T00:00:00Z",
                            "last_edited_time": "2026-07-03T04:05:06Z",
                            "fingerprint": "test",
                        }
                    ],
                    "moved": [],
                    "skipped": [],
                },
            )
            args = argparse.Namespace(config=config_path, state=state_path, limit=5, force=False)
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                workflow.command_next(args)

            rendered = output.getvalue()
            self.assertIn("Takeaway: learned something", rendered)
            self.assertIn("Improvement: try another way", rendered)
            self.assertNotIn("收获: learned something", rendered)


class CandidateTimestampDisplayTest(unittest.TestCase):
    def test_candidate_output_shows_added_and_last_edited(self):
        candidate = workflow.Candidate.from_notion_page(source_row(SOURCE_ID, name="timed item", learnings="learned"))
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            workflow.print_preview_candidates([candidate], real_mode=False)

        rendered = output.getvalue()
        self.assertIn("added: 2026-07-01 00:00", rendered)
        self.assertIn("last edited: 2026-07-02 03:04", rendered)
        self.assertNotIn("done:", rendered)
        self.assertNotIn("completed:", rendered)

    def test_candidate_output_handles_missing_last_edited_time(self):
        row = source_row(SOURCE_ID, name="missing edit time")
        row.pop("last_edited_time")
        candidate = workflow.Candidate.from_notion_page(row)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            workflow.print_preview_candidates([candidate], real_mode=False)

        self.assertIn("last edited: unknown", output.getvalue())

    def test_last_edited_time_is_not_part_of_content_fingerprint(self):
        row = source_row(SOURCE_ID, name="same content", learnings="same", improvements="same")
        first = workflow.Candidate.from_notion_page(row)
        row["last_edited_time"] = "2026-07-05T06:07:08Z"
        second = workflow.Candidate.from_notion_page(row)

        self.assertEqual(workflow.candidate_fingerprint(first), workflow.candidate_fingerprint(second))


class CacheAndCooldownTest(unittest.TestCase):
    def test_rate_limit_cooldown_blocks_without_calling_urlopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            workflow.record_rate_limit(
                cache_path,
                '{"additional_data":{"retry_after":"30"}}',
                "/databases/source/query",
            )
            client = workflow.NotionClient("token", cache_path=cache_path)

            with patch("urllib.request.urlopen") as urlopen_mock:
                with self.assertRaises(workflow.WorkflowError):
                    client.retrieve_page("abc")

            urlopen_mock.assert_not_called()

    def test_connector_cooldown_does_not_block_external_api_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            workflow.record_rate_limit(
                cache_path,
                '{"additional_data":{"retry_after":"30"}}',
                "connector source candidate query LIMIT 5",
                mode="connector",
            )
            client = workflow.NotionClient("token", cache_path=cache_path)

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return b'{"id":"abc"}'

            with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen_mock:
                self.assertEqual(client.retrieve_page("abc"), {"id": "abc"})

            urlopen_mock.assert_called_once()

    def test_preflight_uses_fresh_cache_without_notion(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            workflow.mark_preflight_ok(config(), cache_path)

            with patch.object(workflow, "get_client") as get_client_mock:
                workflow.preflight(config(), cache_path=cache_path)

            get_client_mock.assert_not_called()

    def test_make_batch_checks_ledger_before_limited_source_query_without_cache(self):
        source_row = {
            "id": SOURCE_ID,
            "url": f"https://example.invalid/notion/{SOURCE_ID}",
            "created_time": "2026-07-01T00:00:00Z",
            "properties": {
                "完成": {"type": "checkbox", "checkbox": True},
                "Name": {"type": "title", "title": [{"plain_text": "same name"}]},
                "category": rich_text("alpha"),
                "收获": rich_text("learned"),
                "改进": rich_text("improve"),
            },
        }

        class SourceOnlyClient(FakeClient):
            def __init__(self):
                super().__init__([source_row])
                self.query_count = 0
                self.queries = []

            def query_database(self, database_id, start_cursor=None, page_size=100):
                self.query_count += 1
                self.queries.append((database_id, page_size))
                self.queried_database_id = database_id
                self.queried_page_size = page_size
                if database_id == LEDGER_DB:
                    return {"results": [], "has_more": False}
                if database_id == SOURCE_DB:
                    return {"results": [source_row], "has_more": False}
                return {"results": [], "has_more": False}

        client = SourceOnlyClient()
        with patch.object(workflow, "get_client", return_value=client):
            batch = workflow.make_batch(config(), {"moved": [], "skipped": []}, 5)

        self.assertEqual(len(batch), 1)
        self.assertEqual(client.query_count, 3)
        self.assertEqual(client.queries[0], (LEDGER_DB, 100))
        self.assertEqual(client.queries[1], (SOURCE_DB, 15))
        self.assertEqual(client.queries[2], (TARGET_DB, 100))
        self.assertEqual(client.queried_database_id, TARGET_DB)
        self.assertEqual(client.queried_page_size, 100)


class BatchSelectionTest(unittest.TestCase):
    def test_checked_empty_rows_are_removed_in_order_without_counting_toward_batch(self):
        first_empty = source_row("empty-first", name="empty first")
        eligible = source_row("eligible-one", name="eligible", learnings="learned")
        later_empty = source_row("empty-later", name="empty later")

        class OrderedClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.queries = []

            def query_database(self, database_id, start_cursor=None, page_size=100):
                self.queries.append((database_id, page_size))
                if database_id == LEDGER_DB:
                    return {"results": [], "has_more": False}
                if database_id == SOURCE_DB:
                    return {"results": [first_empty, eligible, later_empty], "has_more": False}
                return {"results": [], "has_more": False}

        client = OrderedClient()
        state = {"moved": [], "skipped": [], "batch_id": 3, "session_id": "session-1"}

        with patch.object(workflow, "get_client", return_value=client), patch.object(workflow, "backup_record") as backup:
            batch = workflow.make_batch(config(), state, 1)

        self.assertEqual([item.name for item in batch], ["eligible"])
        self.assertEqual(client.archive_calls, ["empty-first"])
        self.assertEqual(len(state["empty_removed"]), 1)
        self.assertEqual(state["empty_removed"][0]["name_hint"], "empty first")
        self.assertEqual(state["_empty_removed_this_batch"][0]["batch"], 3)
        backup.assert_called_once()

    def test_make_batch_continues_to_next_source_page_after_empty_rows(self):
        empty_page = source_row("empty-page-one", name="empty page one")
        eligible = source_row("eligible-page-two", name="eligible page two", learnings="learned")

        class PagedClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.source_queries = 0

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == LEDGER_DB:
                    return {"results": [], "has_more": False}
                if database_id == SOURCE_DB:
                    self.source_queries += 1
                    if start_cursor is None:
                        return {"results": [empty_page], "has_more": True, "next_cursor": "page-2"}
                    return {"results": [eligible], "has_more": False}
                return {"results": [], "has_more": False}

        client = PagedClient()
        state = {"moved": [], "skipped": [], "batch_id": 4, "session_id": "session-1"}

        with patch.object(workflow, "get_client", return_value=client), patch.object(workflow, "backup_record"):
            batch = workflow.make_batch(config(), state, 1)

        self.assertEqual([item.name for item in batch], ["eligible page two"])
        self.assertEqual(client.archive_calls, ["empty-page-one"])
        self.assertEqual(client.source_queries, 2)

    def test_make_batch_records_manual_match_without_candidate_or_duplicate_copy(self):
        source = source_row(SOURCE_ID, name="manual item", learnings="learned", improvements="better")
        target = archive_row(TARGET_ID, name="manual item", learnings="learned", improvements="better")

        class ManualMatchClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.manual_rows = []

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == LEDGER_DB:
                    return {"results": [], "has_more": False}
                if database_id == SOURCE_DB:
                    return {"results": [source], "has_more": False}
                if database_id == TARGET_DB:
                    return {"results": [target], "has_more": False}
                return {"results": [], "has_more": False}

            def create_manual_match_ledger_row(self, database_id, **kwargs):
                self.manual_rows.append((database_id, kwargs))
                return {"id": "manual-ledger-page-id", "url": "https://example.invalid/notion/manual-ledger-page-id"}

        client = ManualMatchClient()
        state = {"moved": [], "skipped": [], "batch_id": 5, "session_id": "session-1"}

        with patch.object(workflow, "get_client", return_value=client), patch.object(workflow, "backup_record"):
            batch = workflow.make_batch(config(), state, 1)

        self.assertEqual(batch, [])
        self.assertEqual(len(state["manual_matches"]), 1)
        self.assertEqual(state["manual_matches"][0]["target_page_id"], TARGET_ID)
        self.assertEqual(len(state["_manual_matched_this_batch"]), 1)
        self.assertEqual(client.manual_rows[0][0], LEDGER_DB)

    def test_make_batch_does_not_manual_match_when_archive_content_differs(self):
        source = source_row(SOURCE_ID, name="manual item", learnings="learned", improvements="better")
        target = archive_row(TARGET_ID, name="manual item", learnings="old", improvements="better")

        class DifferentArchiveClient(FakeClient):
            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == LEDGER_DB:
                    return {"results": [], "has_more": False}
                if database_id == SOURCE_DB:
                    return {"results": [source], "has_more": False}
                if database_id == TARGET_DB:
                    return {"results": [target], "has_more": False}
                return {"results": [], "has_more": False}

        client = DifferentArchiveClient([])
        state = {"moved": [], "skipped": [], "batch_id": 5, "session_id": "session-1"}

        with patch.object(workflow, "get_client", return_value=client), patch.object(workflow, "backup_record"):
            batch = workflow.make_batch(config(), state, 1)

        self.assertEqual([item.name for item in batch], ["manual item"])
        self.assertEqual(state.get("manual_matches"), None)


class PendingRemovalVisibilityTest(unittest.TestCase):
    def test_pending_moved_sources_still_in_source_are_reported(self):
        moved_source = source_row(SOURCE_ID, name="already categorized", learnings="learned")

        class PendingClient(FakeClient):
            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == LEDGER_DB:
                    return {"results": [ledger_page()], "has_more": False}
                if database_id == SOURCE_DB:
                    return {"results": [moved_source], "has_more": False}
                return {"results": [], "has_more": False}

        pending = workflow.pending_moved_sources_still_in_source(config(), PendingClient([]))

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].name, "already categorized")
        self.assertEqual(pending[0].category, "alpha")
        self.assertTrue(pending[0].completed)

    def test_pending_moved_source_reports_unchecked_completion_state(self):
        moved_source = source_row(SOURCE_ID, name="already categorized but unchecked", done=False, learnings="learned")

        class PendingUncheckedClient(FakeClient):
            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == LEDGER_DB:
                    return {"results": [ledger_page()], "has_more": False}
                if database_id == SOURCE_DB:
                    return {"results": [moved_source], "has_more": False}
                return {"results": [], "has_more": False}

        pending = workflow.pending_moved_sources_still_in_source(config(), PendingUncheckedClient([]))

        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].completed)

    def test_removed_ledger_rows_are_not_reported_as_pending(self):
        moved_source = source_row(SOURCE_ID, name="already removed", learnings="learned")
        removed_ledger = ledger_page()
        removed_ledger["properties"]["stability"] = select("removed")

        class RemovedClient(FakeClient):
            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == LEDGER_DB:
                    return {"results": [removed_ledger], "has_more": False}
                if database_id == SOURCE_DB:
                    return {"results": [moved_source], "has_more": False}
                return {"results": [], "has_more": False}

        pending = workflow.pending_moved_sources_still_in_source(config(), RemovedClient([]))

        self.assertEqual(pending, [])


class RealModePreviewTest(unittest.TestCase):
    def write_json(self, path, data):
        workflow.save_json(path, data)

    def test_real_done_waits_for_confirm_source_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            target_id = "FAKE_CREATED_TARGET_PAGE_ID"
            record = dict(moved_record(target_id=target_id), category="research", name="real item")
            record.pop("ledger_page_id")
            self.write_json(config_path, real_config())
            self.write_json(state_path, {"moved": [record], "manual_matches": [], "skipped": []})
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                workflow.command_done(argparse.Namespace(config=config_path, state=state_path))

            updated = workflow.load_json(state_path, {})
            self.assertTrue(updated["awaiting_done_confirm"])
            self.assertIn("Confirm today's changes?", output.getvalue())
            self.assertNotIn("Real mode source cleanup is not enabled yet", output.getvalue())

    def test_real_confirm_requires_saved_archive_table_row_before_source_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            source = real_source_row(SOURCE_ID, name="real item", categories=[RESEARCH_PROJECT_ID], learnings="learned")
            self.write_json(config_path, real_config())
            self.write_json(
                state_path,
                {
                    "batch": [
                        {
                            "source_page_id": SOURCE_ID,
                            "source_url": f"https://example.invalid/notion/{SOURCE_ID}",
                            "name": "real item",
                            "category": "alpha",
                            "learnings": "learned",
                            "improvements": "",
                            "created_time": "2026-07-01T00:00:00Z",
                            "fingerprint": "test",
                        }
                    ],
                    "moved": [dict(moved_record(target_id=RESEARCH_ARCHIVE_DB), category="research", name="real item")],
                    "skipped": [],
                    "awaiting_done_confirm": True,
                },
            )

            class CleanupClient(FakeClient):
                def __init__(self):
                    super().__init__([])
                    self.archive_tables_content = archive_tables_content()

                def retrieve_page(self, page_id):
                    if workflow.notion_id(page_id) == SOURCE_ID:
                        return source
                    return super().retrieve_page(page_id)

            output = io.StringIO()
            client = CleanupClient()

            with patch.object(workflow, "get_client", return_value=client), contextlib.redirect_stdout(output):
                workflow.command_confirm(argparse.Namespace(config=config_path, state=state_path))

            self.assertIn("current source content is not saved in the target archive database row", output.getvalue())
            self.assertEqual(client.archive_calls, [])

    def test_real_confirm_finalizes_without_notion_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            target_id = "FAKE_CREATED_TARGET_PAGE_ID"
            source = real_source_row(SOURCE_ID, name="real item", categories=[RESEARCH_PROJECT_ID], learnings="learned")
            target = archive_row(target_id, name="real item", learnings="learned")
            target["parent"]["database_id"] = RESEARCH_ARCHIVE_DB
            record = dict(moved_record(target_id=target_id), category="research", name="real item")
            record.pop("ledger_page_id")
            self.write_json(config_path, real_config())
            self.write_json(
                state_path,
                {
                    "moved": [record],
                    "manual_matches": [],
                    "skipped": [],
                    "awaiting_done_confirm": True,
                },
            )

            class CleanupClient(FakeClient):
                def __init__(self):
                    super().__init__([])
                    self.archive_tables_content = archive_tables_content()

                def retrieve_page(self, page_id):
                    compact = workflow.notion_id(page_id)
                    if compact == SOURCE_ID:
                        return source
                    if compact == target_id:
                        return target
                    return super().retrieve_page(page_id)

            output = io.StringIO()
            client = CleanupClient()

            with (
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "run_backup", return_value="session-1"),
                contextlib.redirect_stdout(output),
            ):
                workflow.command_confirm(argparse.Namespace(config=config_path, state=state_path))

            updated = workflow.load_json(state_path, {})
            self.assertEqual(client.archive_calls, [SOURCE_ID])
            self.assertEqual(client.ledger_updates, [])
            self.assertEqual(updated["moved"], [])
            self.assertEqual(len(updated["removed"]), 1)

    def test_real_confirm_does_not_cleanup_undone_or_skipped_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            target_id = "FAKE_CREATED_TARGET_PAGE_ID"
            source = real_source_row(SOURCE_ID, name="real item", categories=[RESEARCH_PROJECT_ID], learnings="learned")
            target = archive_row(target_id, name="real item", learnings="learned")
            target["parent"]["database_id"] = RESEARCH_ARCHIVE_DB
            record = dict(moved_record(target_id=target_id), category="research", name="real item")
            record.pop("ledger_page_id")
            self.write_json(config_path, real_config())
            self.write_json(
                state_path,
                {
                    "moved": [record],
                    "manual_matches": [],
                    "skipped": [{"source_page_id": SOURCE_ID}],
                    "undone": [record],
                    "awaiting_done_confirm": True,
                },
            )

            class CleanupClient(FakeClient):
                def __init__(self):
                    super().__init__([])
                    self.archive_tables_content = archive_tables_content()

                def retrieve_page(self, page_id):
                    compact = workflow.notion_id(page_id)
                    if compact == SOURCE_ID:
                        return source
                    if compact == target_id:
                        return target
                    return super().retrieve_page(page_id)

            output = io.StringIO()
            client = CleanupClient()

            with (
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "run_backup", return_value="session-1"),
                contextlib.redirect_stdout(output),
            ):
                workflow.command_confirm(argparse.Namespace(config=config_path, state=state_path))

            updated = workflow.load_json(state_path, {})
            self.assertEqual(client.archive_calls, [])
            self.assertTrue(updated["awaiting_done_confirm"])
            self.assertEqual(len(updated["moved"]), 1)
            self.assertEqual(len(updated["needs_review"]), 1)
            self.assertIn("undone", updated["needs_review"][0]["needs_review"])

    def test_preview_reports_would_do_items_without_writes(self):
        empty = real_source_row("empty-real", name="empty real", categories=[RESEARCH_PROJECT_ID])
        manual_source = real_source_row(
            SOURCE_ID,
            name="manual real",
            categories=[RESEARCH_PROJECT_ID, WRITING_PROJECT_ID],
            learnings="learned",
            improvements="better",
        )
        manual_target = real_archive_row(
            TARGET_ID,
            name="manual real",
            categories=[RESEARCH_PROJECT_ID, WRITING_PROJECT_ID],
            learnings="learned",
            improvements="better",
        )
        eligible = real_source_row(
            "eligible-real",
            name="eligible real",
            categories=[RESEARCH_PROJECT_ID, WRITING_PROJECT_ID],
            learnings="new",
        )

        class PreviewClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.archive_tables_content = archive_tables_content()
                self.manual_rows = []
                self.archive_rows = []
                self.archive_database_queries = []
                self.archive_table_content_reads = 0

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == LEDGER_DB:
                    return {"results": [], "has_more": False}
                if database_id == SOURCE_DB:
                    return {"results": [empty, manual_source, eligible], "has_more": False}
                if database_id in {RESEARCH_ARCHIVE_DB, WRITING_ARCHIVE_DB}:
                    self.archive_database_queries.append(database_id)
                    return {"results": [manual_target], "has_more": False}
                return {"results": [], "has_more": False}

            def retrieve_archive_tables_content(self, page_id):
                self.archive_table_content_reads += 1
                return self.archive_tables_content

            def create_archive_row(self, database_id, candidate):
                self.archive_rows.append((database_id, candidate))
                return {"id": "created"}

            def create_manual_match_ledger_row(self, database_id, **kwargs):
                self.manual_rows.append((database_id, kwargs))
                return {"id": "manual"}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, real_config())
            self.write_json(state_path, {"batch": [], "moved": [], "skipped": []})
            args = argparse.Namespace(config=config_path, state=state_path, batch_size=5, move_limit=None)
            client = PreviewClient()
            output = io.StringIO()

            with patch.object(workflow, "preflight"), patch.object(workflow, "get_client", return_value=client):
                with contextlib.redirect_stdout(output):
                    workflow.command_preview(args)

            rendered = output.getvalue()
            self.assertIn("Mode: real read-only", rendered)
            self.assertIn("Preview only", rendered)
            self.assertIn("Would remove empty completed rows", rendered)
            self.assertIn("[research] empty real", rendered)
            self.assertIn("[research + writing] manual real", rendered)
            self.assertIn("eligible real", rendered)
            self.assertIn("added: 2026-07-01 00:00", rendered)
            self.assertIn("ok 1 to research", rendered)
            self.assertIn("ok 1 to writing", rendered)
            self.assertIn("ok 1 to all", rendered)
            self.assertIn("dismiss 1", rendered)
            self.assertIn("skip 1", rendered)
            self.assertLess(rendered.index("ok 1 to all"), rendered.index("dismiss 1"))
            self.assertLess(rendered.index("dismiss 1"), rendered.index("skip 1"))
            self.assertIn("Manual-match will be checked only when you approve", rendered)
            self.assertEqual(client.archive_calls, [])
            self.assertEqual(client.archive_rows, [])
            self.assertEqual(client.manual_rows, [])
            self.assertEqual(client.archive_database_queries, [])
            self.assertEqual(client.archive_table_content_reads, 0)
            self.assertEqual(workflow.load_json(state_path, {})["batch"], [])

    def test_preview_single_category_prints_simple_ok_choice(self):
        eligible = real_source_row("eligible-real", name="eligible real", categories=[RESEARCH_PROJECT_ID], learnings="new")

        class PreviewClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.archive_tables_content = archive_tables_content()

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == LEDGER_DB:
                    return {"results": [], "has_more": False}
                if database_id == SOURCE_DB:
                    return {"results": [eligible], "has_more": False}
                if database_id == RESEARCH_ARCHIVE_DB:
                    return {"results": [], "has_more": False}
                return {"results": [], "has_more": False}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, real_config())
            self.write_json(state_path, {"batch": [], "moved": [], "skipped": []})
            args = argparse.Namespace(config=config_path, state=state_path, batch_size=5, move_limit=None)
            output = io.StringIO()

            with patch.object(workflow, "preflight"), patch.object(workflow, "get_client", return_value=PreviewClient()):
                with contextlib.redirect_stdout(output):
                    workflow.command_preview(args)

            rendered = output.getvalue()
            self.assertIn("[research] eligible real", rendered)
            self.assertIn("added: 2026-07-01 00:00", rendered)
            self.assertIn("ok 1", rendered)
            self.assertIn("dismiss 1", rendered)
            self.assertIn("skip 1", rendered)
            self.assertLess(rendered.index("ok 1"), rendered.index("dismiss 1"))
            self.assertLess(rendered.index("dismiss 1"), rendered.index("skip 1"))
            self.assertNotIn("ok 1 to all", rendered)

    def test_real_ok_parser_requires_choice_for_multi_category_items(self):
        batch = [
            workflow.Candidate(
                source_page_id=SOURCE_ID,
                source_url="https://example.invalid/notion/source",
                name="multi",
                category="research + writing",
                learnings="learned",
                improvements="",
                created_time="",
                category_options=["research", "writing"],
            )
        ]

        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.parse_ok_items(["1"], batch, real_mode=True)

        self.assertIn("ok 1 to research", str(cm.exception))
        self.assertEqual(
            workflow.parse_ok_items(["1", "to", "all"], batch, real_mode=True),
            [{"number": 1, "selected_categories": ["research", "writing"]}],
        )

    def test_real_ok_parser_supports_category_names_with_spaces(self):
        batch = [
            workflow.Candidate(
                source_page_id=SOURCE_ID,
                source_url="https://example.invalid/notion/source",
                name="multi",
                category="research + admin",
                learnings="learned",
                improvements="",
                created_time="",
                category_options=["research", "admin"],
            )
        ]

        self.assertEqual(
            workflow.parse_ok_items(["1", "to", "admin"], batch, real_mode=True),
            [{"number": 1, "selected_categories": ["admin"]}],
        )

    def test_real_archive_target_missing_suggests_similar_toggle_database(self):
        cfg = dict(real_config())
        cfg["archive_tables"] = {"example-topic": "Example Topic Archive"}

        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.require_archive_table_target(cfg, archive_tables_content() + """
<details>
<summary>Example Topic</summary>
\t<database url="https://example.invalid/notion/FAKE_EXAMPLE_TOPIC_DATABASE_ID" inline="true" data-source-url="collection://FAKE_EXAMPLE_TOPIC_DATA_SOURCE_ID">Example Topic</database>
</details>
""", "example-topic")

        self.assertIn("similar table", str(cm.exception))
        self.assertIn("Example Topic", str(cm.exception))

    def test_real_archive_target_missing_without_similar_requests_new_toggle_database(self):
        cfg = dict(real_config())
        cfg["archive_tables"] = {"new category": "new category"}

        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.require_archive_table_target(cfg, archive_tables_content(), "new category")

        self.assertIn("create a new toggle-table pair", str(cm.exception))

    def test_real_preview_can_stage_batch_locally_for_trial_write(self):
        eligible = real_source_row("eligible-real", name="eligible real", categories=[RESEARCH_PROJECT_ID], learnings="new")

        class PreviewClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.archive_tables_content = archive_tables_content()

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == SOURCE_DB:
                    return {"results": [eligible], "has_more": False}
                if database_id == TARGET_DB:
                    return {"results": [], "has_more": False}
                return {"results": [], "has_more": False}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, real_config())
            self.write_json(state_path, {"batch": [], "moved": [], "skipped": []})
            args = argparse.Namespace(config=config_path, state=state_path, batch_size=5, move_limit=None, stage_real_trial=True)

            with (
                patch.object(workflow, "preflight"),
                patch.object(workflow, "get_client", return_value=PreviewClient()),
                patch.object(workflow, "run_backup", return_value="session-1"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                workflow.command_preview(args)

            state = workflow.load_json(state_path, {})
            self.assertEqual(len(state["batch"]), 1)
            self.assertEqual(state["batch"][0]["source_page_id"], "eligible-real")
            self.assertEqual(state["batch_id"], 1)

    def test_real_next_stages_formal_batch_without_removing_empty_rows(self):
        empty = real_source_row("empty-real", name="empty real", categories=[RESEARCH_PROJECT_ID])
        eligible = real_source_row("eligible-real", name="eligible real", categories=[RESEARCH_PROJECT_ID], learnings="new")

        class NextClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.archive_tables_content = archive_tables_content()

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == SOURCE_DB:
                    return {"results": [empty, eligible], "has_more": False}
                if database_id == TARGET_DB:
                    return {"results": [], "has_more": False}
                return {"results": [], "has_more": False}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, real_config())
            self.write_json(state_path, {"batch": [], "moved": [], "skipped": []})
            args = argparse.Namespace(config=config_path, state=state_path, batch_size=5, move_limit=None, force=False)
            client = NextClient()
            output = io.StringIO()

            with (
                patch.object(workflow, "preflight"),
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "run_backup", return_value="session-1"),
                contextlib.redirect_stdout(output),
            ):
                workflow.command_next(args)

            state = workflow.load_json(state_path, {})
            rendered = output.getvalue()
            self.assertEqual(len(state["batch"]), 1)
            self.assertEqual(state["batch"][0]["source_page_id"], "eligible-real")
            self.assertEqual(client.archive_calls, [])
            self.assertIn("Would remove empty completed rows", rendered)
            self.assertIn("ok 1", rendered)

    def test_real_next_reply_example_uses_configured_candidate_category(self):
        eligible = real_source_row("eligible-real", name="eligible real", categories=[WRITING_PROJECT_ID], learnings="new")

        class NextClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.archive_tables_content = archive_tables_content()

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == SOURCE_DB:
                    return {"results": [eligible], "has_more": False}
                return {"results": [], "has_more": False}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, real_config())
            self.write_json(state_path, {"batch": [], "moved": [], "skipped": []})
            args = argparse.Namespace(config=config_path, state=state_path, batch_size=5, move_limit=None, force=False)
            output = io.StringIO()

            with (
                patch.object(workflow, "preflight"),
                patch.object(workflow, "get_client", return_value=NextClient()),
                patch.object(workflow, "run_backup", return_value="session-1"),
                contextlib.redirect_stdout(output),
            ):
                workflow.command_next(args)

        rendered = output.getvalue()
        self.assertIn("ok 1 to writing", rendered)
        self.assertNotIn("ok 1 to research", rendered)

    def test_real_ok_requires_category_choice_for_multi_category_without_trial_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            source = real_source_row(SOURCE_ID, name="multi real", categories=[RESEARCH_PROJECT_ID, WRITING_PROJECT_ID], learnings="learned")
            snapshot = workflow.real_candidate_from_notion_page(source, real_config())
            self.write_json(config_path, real_config())
            self.write_json(state_path, {"batch": [snapshot.to_state()], "moved": [], "skipped": []})
            args = argparse.Namespace(config=config_path, state=state_path, items=["1"], real_write_confirm=None)

            with self.assertRaises(workflow.WorkflowError) as cm:
                workflow.command_ok(args)

            self.assertIn("ok 1 to research", str(cm.exception))

    def test_real_ok_appends_only_three_properties_to_selected_archive_database(self):
        source = real_source_row(
            SOURCE_ID,
            name="example multi-category task",
            categories=[RESEARCH_PROJECT_ID, WRITING_PROJECT_ID],
            learnings="example takeaway",
        )
        snapshot = workflow.real_candidate_from_notion_page(source, real_config())

        class RealWriteClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.archive_tables_content = archive_tables_content()

            def retrieve_page(self, page_id):
                if workflow.notion_id(page_id) == SOURCE_ID:
                    return source
                return super().retrieve_page(page_id)

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == RESEARCH_ARCHIVE_DB:
                    return {"results": [], "has_more": False}
                return {"results": [], "has_more": False}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, real_config())
            self.write_json(
                state_path,
                {
                    "batch": [snapshot.to_state()],
                    "batch_id": 1,
                    "moved": [],
                    "skipped": [],
                    "backup_session_id": "session-1",
                    "session_id": "session-1",
                },
            )
            client = RealWriteClient()
            args = argparse.Namespace(
                config=config_path,
                state=state_path,
                items=["1", "to", "research"],
                real_write_confirm=None,
            )

            with (
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "run_backup", return_value="session-1"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                workflow.command_ok(args)

            self.assertEqual(len(client.created_archive_rows), 1)
            database_id, candidate, kwargs = client.created_archive_rows[0]
            self.assertEqual(database_id, RESEARCH_ARCHIVE_DB)
            self.assertEqual(candidate.category, "research")
            self.assertEqual(kwargs, {})
            self.assertEqual(client.safety_log_rows, [])
            state = workflow.load_json(state_path, {})
            self.assertEqual(state["moved"][0]["category"], "research")
            self.assertNotIn("safety_log_page_id", state["moved"][0])

    def test_real_ok_detects_manual_match_after_light_preview(self):
        source = real_source_row(
            SOURCE_ID,
            name="manual real",
            categories=[RESEARCH_PROJECT_ID],
            learnings="learned",
            improvements="better",
        )
        target = archive_row(TARGET_ID, name="manual real", learnings="learned", improvements="better")
        target["parent"]["database_id"] = RESEARCH_ARCHIVE_DB
        snapshot = workflow.real_candidate_from_notion_page(source, real_config())

        class ManualMatchClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.archive_tables_content = archive_tables_content()
                self.archive_database_queries = []

            def retrieve_page(self, page_id):
                if workflow.notion_id(page_id) == SOURCE_ID:
                    return source
                return super().retrieve_page(page_id)

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == RESEARCH_ARCHIVE_DB:
                    self.archive_database_queries.append(database_id)
                    return {"results": [target], "has_more": False}
                return {"results": [], "has_more": False}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, real_config())
            self.write_json(
                state_path,
                {
                    "batch": [snapshot.to_state()],
                    "batch_id": 1,
                    "moved": [],
                    "skipped": [],
                    "backup_session_id": "session-1",
                    "session_id": "session-1",
                },
            )
            client = ManualMatchClient()

            with (
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "run_backup", return_value="session-1"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                workflow.command_ok(argparse.Namespace(config=config_path, state=state_path, items=["1", "to", "research"], real_write_confirm=None))

            state = workflow.load_json(state_path, {})
            self.assertEqual(client.created_archive_rows, [])
            self.assertEqual(client.archive_database_queries, [RESEARCH_ARCHIVE_DB])
            self.assertEqual(len(state["manual_matches"]), 1)
            self.assertEqual(state["manual_matches"][0]["target_page_id"], TARGET_ID)

    def test_real_ok_reuses_archive_database_scan_for_same_category(self):
        source_one_id = SOURCE_ID
        source_two_id = "FAKE_SECOND_SOURCE_PAGE_ID"
        source_one = real_source_row(source_one_id, name="first real", categories=[RESEARCH_PROJECT_ID], learnings="one")
        source_two = real_source_row(source_two_id, name="second real", categories=[RESEARCH_PROJECT_ID], learnings="two")
        snapshot_one = workflow.real_candidate_from_notion_page(source_one, real_config())
        snapshot_two = workflow.real_candidate_from_notion_page(source_two, real_config())

        class ReuseClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.archive_tables_content = archive_tables_content()
                self.archive_database_queries = []

            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == source_one_id:
                    return source_one
                if compact == source_two_id:
                    return source_two
                return super().retrieve_page(page_id)

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id == RESEARCH_ARCHIVE_DB:
                    self.archive_database_queries.append(database_id)
                    return {"results": [], "has_more": False}
                return {"results": [], "has_more": False}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, real_config())
            self.write_json(
                state_path,
                {
                    "batch": [snapshot_one.to_state(), snapshot_two.to_state()],
                    "batch_id": 1,
                    "moved": [],
                    "skipped": [],
                    "backup_session_id": "session-1",
                    "session_id": "session-1",
                },
            )
            client = ReuseClient()

            with (
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "run_backup", return_value="session-1"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                workflow.command_ok(argparse.Namespace(config=config_path, state=state_path, items=["1", "2"], real_write_confirm=None))

            self.assertEqual(client.archive_database_queries, [RESEARCH_ARCHIVE_DB])
            self.assertEqual([row[0] for row in client.created_archive_rows], [RESEARCH_ARCHIVE_DB, RESEARCH_ARCHIVE_DB])

    def test_real_config_rejects_notion_log_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"

            self.write_json(config_path, dict(real_config(), safety_log_parent_page_id="page-id"))
            with self.assertRaisesRegex(workflow.WorkflowError, "Real mode must not write Notion safety/log/ledger pages"):
                workflow.load_config(config_path)

            self.write_json(config_path, dict(real_config(), ledger_database_id=LEDGER_DB))
            with self.assertRaisesRegex(workflow.WorkflowError, "Real mode must not write Notion safety/log/ledger pages"):
                workflow.load_config(config_path)

    def test_real_ok_to_all_appends_one_row_per_selected_category_table(self):
        source = real_source_row(
            SOURCE_ID,
            name="multi real",
            categories=[RESEARCH_PROJECT_ID, WRITING_PROJECT_ID],
            learnings="ready",
        )
        snapshot = workflow.real_candidate_from_notion_page(source, real_config())

        class RealWriteClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.archive_tables_content = archive_tables_content()

            def retrieve_page(self, page_id):
                if workflow.notion_id(page_id) == SOURCE_ID:
                    return source
                return super().retrieve_page(page_id)

            def query_database(self, database_id, start_cursor=None, page_size=100):
                if database_id in {RESEARCH_ARCHIVE_DB, WRITING_ARCHIVE_DB}:
                    return {"results": [], "has_more": False}
                return {"results": [], "has_more": False}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "real_profile.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, real_config())
            self.write_json(
                state_path,
                {
                    "batch": [snapshot.to_state()],
                    "batch_id": 1,
                    "moved": [],
                    "skipped": [],
                    "backup_session_id": "session-1",
                    "session_id": "session-1",
                },
            )
            client = RealWriteClient()
            args = argparse.Namespace(config=config_path, state=state_path, items=["1", "to", "all"], real_write_confirm=None)

            with (
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "run_backup", return_value="session-1"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                workflow.command_ok(args)

            self.assertEqual([row[0] for row in client.created_archive_rows], [RESEARCH_ARCHIVE_DB, WRITING_ARCHIVE_DB])
            state = workflow.load_json(state_path, {})
            self.assertEqual([record["category"] for record in state["moved"]], ["research", "writing"])



class DestructiveActionLedgerValidationTest(unittest.TestCase):
    def write_json(self, path, data):
        workflow.save_json(path, data)

    def run_with_files(self, client, state_data, command):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, config())
            self.write_json(state_path, state_data)
            args = argparse.Namespace(config=config_path, state=state_path, items=["1"], confirm="REMOVE_SOURCES")
            with patch.object(workflow, "get_client", return_value=client), patch.object(workflow, "backup_record"):
                command(args)
            return workflow.load_json(state_path, {})

    def test_dismiss_records_pending_source_removal_without_archive_row(self):
        source_candidate = workflow.Candidate.from_notion_page(page(SOURCE_ID, SOURCE_DB))
        client = FakeClient([ledger_page()])
        state = {
            "batch_id": 1,
            "batch": [source_candidate.to_state()],
            "moved": [],
            "skipped": [],
            "backup_session_id": "session-1",
            "session_id": "session-1",
        }

        updated = self.run_with_files(client, state, workflow.command_dismiss)

        self.assertEqual(client.created_archive_rows, [])
        self.assertEqual(client.archive_calls, [])
        self.assertEqual(len(updated["dismissed"]), 1)
        self.assertEqual(updated["dismissed"][0]["source_page_id"], SOURCE_ID)
        self.assertEqual(updated["dismissed"][0]["fingerprint"], workflow.candidate_fingerprint(source_candidate))

    def test_undo_dismiss_restores_item_without_notion_write(self):
        source_candidate = workflow.Candidate.from_notion_page(page(SOURCE_ID, SOURCE_DB))
        state = {
            "batch_id": 1,
            "batch": [source_candidate.to_state()],
            "dismissed": [dismissed_record()],
            "moved": [],
            "skipped": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, config())
            self.write_json(state_path, state)
            args = argparse.Namespace(config=config_path, state=state_path, items=["1"], confirm="REMOVE_SOURCES")
            output = io.StringIO()

            with (
                patch.object(workflow, "get_client") as get_client,
                patch.object(workflow, "backup_record"),
                contextlib.redirect_stdout(output),
            ):
                workflow.command_undo(args)

            updated = workflow.load_json(state_path, {})

        get_client.assert_not_called()
        self.assertEqual(updated["dismissed"], [])
        self.assertEqual(len(updated["undone"]), 1)
        self.assertIn("Batch still has unhandled items: 1", output.getvalue())

    def test_done_waits_for_confirmation_when_only_dismissed(self):
        client = FakeClient([])
        state = {"dismissed": [dismissed_record()], "moved": [], "manual_matches": [], "skipped": []}

        updated = self.run_with_files(client, state, workflow.command_done)

        self.assertTrue(updated["awaiting_done_confirm"])
        self.assertEqual(client.archive_calls, [])

    def test_confirm_finalizes_dismissed_source_without_target_or_ledger(self):
        client = FakeClient([])
        state = {"dismissed": [dismissed_record()], "moved": [], "skipped": [], "awaiting_done_confirm": True}

        updated = self.run_with_files(client, state, workflow.command_confirm)

        self.assertEqual(client.archive_calls, [SOURCE_ID])
        self.assertEqual(client.ledger_updates, [])
        self.assertEqual(updated["dismissed"], [])
        self.assertEqual(len(updated["removed"]), 1)

    def test_remove_sources_rejects_dismissed_without_done_confirm_path(self):
        client = FakeClient([])
        state = {"dismissed": [dismissed_record()], "moved": [], "skipped": []}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, config())
            self.write_json(state_path, state)
            args = argparse.Namespace(config=config_path, state=state_path, confirm="REMOVE_SOURCES")

            with (
                patch.object(workflow, "get_client", return_value=client),
                self.assertRaisesRegex(workflow.WorkflowError, "normal `done` -> `confirm` cleanup path"),
            ):
                workflow.command_remove_sources(args)

        self.assertEqual(client.archive_calls, [])

    def test_confirm_keeps_dismissed_source_when_content_changed(self):
        class ChangedSourceClient(FakeClient):
            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == SOURCE_ID:
                    return source_row(SOURCE_ID, name="changed item", learnings="new")
                return super().retrieve_page(page_id)

        client = ChangedSourceClient([])
        state = {"dismissed": [dismissed_record()], "moved": [], "skipped": [], "awaiting_done_confirm": True}

        updated = self.run_with_files(client, state, workflow.command_confirm)

        self.assertEqual(client.archive_calls, [])
        self.assertTrue(updated["awaiting_done_confirm"])
        self.assertEqual(len(updated["dismissed"]), 1)
        self.assertIn("changed after dismissal", updated["needs_review"][0]["needs_review"])

    def test_done_waits_for_confirmation_without_finalizing_sources(self):
        client = FakeClient([ledger_page()])
        state = {"moved": [moved_record()], "skipped": []}

        updated = self.run_with_files(client, state, workflow.command_done)

        self.assertEqual(client.archive_calls, [])
        self.assertEqual(client.ledger_updates, [])
        self.assertTrue(updated["awaiting_done_confirm"])
        self.assertEqual(len(updated["moved"]), 1)

    def test_confirm_finalizes_checked_pending_sources_after_done(self):
        client = FakeClient([ledger_page()])
        state = {"moved": [moved_record()], "skipped": [], "awaiting_done_confirm": True}

        updated = self.run_with_files(client, state, workflow.command_confirm)

        self.assertEqual(client.archive_calls, [SOURCE_ID])
        self.assertEqual(client.ledger_updates, [("ledger-page-id", "removed")])
        self.assertFalse(updated["awaiting_done_confirm"])
        self.assertEqual(updated["moved"], [])
        self.assertEqual(len(updated["removed"]), 1)

    def test_confirm_does_not_finalize_unchecked_pending_sources(self):
        class UncheckedSourceClient(FakeClient):
            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == SOURCE_ID:
                    return {
                        "id": SOURCE_ID,
                        "archived": False,
                        "parent": {"type": "database_id", "database_id": SOURCE_DB},
                        "properties": {
                            "完成": {"type": "checkbox", "checkbox": False},
                        },
                    }
                if compact == TARGET_ID:
                    return page(TARGET_ID, TARGET_DB)
                return page(compact, "unexpected-database")

        client = UncheckedSourceClient([ledger_page()])
        state = {"moved": [], "skipped": [], "awaiting_done_confirm": True}

        updated = self.run_with_files(client, state, workflow.command_confirm)

        self.assertEqual(client.archive_calls, [])
        self.assertEqual(client.ledger_updates, [])
        self.assertEqual(updated.get("removed"), None)

    def test_confirm_keeps_waiting_when_pending_rows_need_review(self):
        class ChangedSourceClient(FakeClient):
            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == SOURCE_ID:
                    return source_row(SOURCE_ID, name="changed item", learnings="new")
                if compact == TARGET_ID:
                    return page(TARGET_ID, TARGET_DB)
                return page(compact, "unexpected-database")

        client = ChangedSourceClient([ledger_page()])
        state = {"moved": [moved_record()], "awaiting_done_confirm": True}

        updated = self.run_with_files(client, state, workflow.command_confirm)

        self.assertTrue(updated["awaiting_done_confirm"])
        self.assertEqual(len(updated["moved"]), 1)
        self.assertEqual(len(updated["needs_review"]), 1)

    def test_status_shows_done_confirmation_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            self.write_json(state_path, {"awaiting_done_confirm": True})
            args = argparse.Namespace(state=state_path)
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                workflow.command_status(args)

            self.assertIn("awaiting done confirm: True", output.getvalue())

    def test_check_reports_clean_local_state_without_pending_notions_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, config())
            self.write_json(state_path, {"batch": [], "moved": [], "skipped": []})
            args = argparse.Namespace(config=config_path, state=state_path)
            output = io.StringIO()

            with patch.object(workflow, "preflight"), patch.object(workflow, "get_client") as get_client:
                with contextlib.redirect_stdout(output):
                    workflow.command_check(args)

            get_client.assert_not_called()
            rendered = output.getvalue()
            self.assertIn("schema: ok", rendered)
            self.assertIn("pending safety: ok", rendered)

    def test_check_reports_pending_needs_review(self):
        class ChangedSourceClient(FakeClient):
            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == SOURCE_ID:
                    return source_row(SOURCE_ID, name="changed item", learnings="new")
                if compact == TARGET_ID:
                    return page(TARGET_ID, TARGET_DB)
                return page(compact, "unexpected-database")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, config())
            self.write_json(state_path, {"moved": [moved_record()]})
            args = argparse.Namespace(config=config_path, state=state_path)
            output = io.StringIO()

            with patch.object(workflow, "preflight"), patch.object(workflow, "get_client", return_value=ChangedSourceClient([ledger_page()])):
                with contextlib.redirect_stdout(output):
                    workflow.command_check(args)

            self.assertIn("pending safety: 1 needs-review", output.getvalue())

    def test_undo_rejects_forged_target_not_present_in_ledger(self):
        client = FakeClient([ledger_page(target_id=TARGET_ID)])
        state = {"batch_id": 1, "batch": [{}], "moved": [moved_record(target_id="FAKE_CREATED_TARGET_PAGE_ID")]}

        with self.assertRaises(workflow.WorkflowError):
            self.run_with_files(client, state, workflow.command_undo)

        self.assertEqual(client.archive_calls, [])

    def test_undo_archives_target_when_ledger_and_scope_match(self):
        client = FakeClient([ledger_page()])
        state = {"batch_id": 1, "batch": [{}], "moved": [moved_record()]}

        updated = self.run_with_files(client, state, workflow.command_undo)

        self.assertEqual(client.archive_calls, [TARGET_ID])
        self.assertEqual(updated["moved"], [])
        self.assertEqual(len(updated["undone"]), 1)

    def test_ok_can_reapply_same_batch_number_after_undo(self):
        source_candidate = workflow.Candidate.from_notion_page(page(SOURCE_ID, SOURCE_DB))
        op_id = workflow.operation_id("session-1", SOURCE_ID, 1)
        old_record = dict(moved_record(), operation_id=op_id)
        client = FakeClient([])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, {"source_database_id": SOURCE_DB, "target_databases": {"alpha": TARGET_DB}})
            self.write_json(
                state_path,
                {
                    "batch_id": 1,
                    "batch": [source_candidate.to_state()],
                    "moved": [old_record],
                    "undone": [old_record],
                    "skipped": [],
                    "backup_session_id": "session-1",
                    "session_id": "session-1",
                },
            )
            args = argparse.Namespace(config=config_path, state=state_path, items=["1"], real_write_confirm=None)
            output = io.StringIO()

            with (
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "backup_record"),
                contextlib.redirect_stdout(output),
            ):
                workflow.command_ok(args)

            updated = workflow.load_json(state_path, {})

        self.assertEqual(len(client.created_archive_rows), 1)
        self.assertEqual(client.created_archive_rows[0][0], TARGET_DB)
        self.assertEqual(len(updated["moved"]), 2)
        self.assertEqual(updated["moved"][-1]["source_page_id"], SOURCE_ID)
        self.assertNotEqual(updated["moved"][-1]["operation_id"], op_id)
        self.assertIn("Batch complete.", output.getvalue())

    def test_ok_prompts_next_when_batch_is_complete(self):
        source_candidate = workflow.Candidate.from_notion_page(page(SOURCE_ID, SOURCE_DB))
        client = FakeClient([])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, {"source_database_id": SOURCE_DB, "target_databases": {"alpha": TARGET_DB}})
            self.write_json(
                state_path,
                {
                    "batch_id": 1,
                    "batch": [source_candidate.to_state()],
                    "moved": [],
                    "skipped": [],
                    "backup_session_id": "session-1",
                    "session_id": "session-1",
                },
            )
            args = argparse.Namespace(config=config_path, state=state_path, items=["1"], real_write_confirm=None)
            output = io.StringIO()

            with (
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "backup_record"),
                contextlib.redirect_stdout(output),
            ):
                workflow.command_ok(args)

        self.assertIn("Batch complete.", output.getvalue())
        self.assertIn("Reply `next`", output.getvalue())
        self.assertIn("You can still reply `undo N`", output.getvalue())

    def test_skip_records_current_batch_pointer_and_prompts_next_when_complete(self):
        source_candidate = workflow.Candidate.from_notion_page(page(SOURCE_ID, SOURCE_DB))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            self.write_json(
                state_path,
                {
                    "batch_id": 4,
                    "batch": [source_candidate.to_state()],
                    "moved": [],
                    "skipped": [],
                    "backup_session_id": "session-1",
                    "session_id": "session-1",
                },
            )
            args = argparse.Namespace(state=state_path, items=["1"])
            output = io.StringIO()

            with (
                patch.object(workflow, "ensure_backup_session", return_value="session-1"),
                patch.object(workflow, "backup_record") as backup_record,
                contextlib.redirect_stdout(output),
            ):
                workflow.command_skip(args)

            updated = workflow.load_json(state_path, {})

        self.assertEqual(
            updated["skipped"],
            [{"batch_id": 4, "batch_number": 1, "source_page_id": SOURCE_ID}],
        )
        backup_record.assert_called_once()
        backup_payload = backup_record.call_args.args[1]
        self.assertEqual(backup_payload["action"], "skipped")
        self.assertEqual(backup_payload["source_page_id"], SOURCE_ID)
        self.assertIn("skip 1: [alpha] moved item", output.getvalue())
        self.assertIn("Batch complete.", output.getvalue())
        self.assertIn("Reply `next`", output.getvalue())

    def test_undo_prompts_rehandling_of_undone_item(self):
        client = FakeClient([ledger_page()])
        state = {"batch_id": 1, "batch": [workflow.Candidate.from_notion_page(page(SOURCE_ID, SOURCE_DB)).to_state()], "moved": [moved_record()]}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, config())
            self.write_json(state_path, state)
            args = argparse.Namespace(config=config_path, state=state_path, items=["1"], confirm="REMOVE_SOURCES")
            output = io.StringIO()

            with (
                patch.object(workflow, "get_client", return_value=client),
                patch.object(workflow, "backup_record"),
                contextlib.redirect_stdout(output),
            ):
                workflow.command_undo(args)

        self.assertIn("Batch still has unhandled items: 1", output.getvalue())
        self.assertIn("ok N", output.getvalue())

    def test_next_replaces_completed_batch_without_force(self):
        completed = workflow.Candidate.from_notion_page(page(SOURCE_ID, SOURCE_DB))
        new_candidate = workflow.Candidate(
            source_page_id="new-source-id",
            source_url="https://example.invalid/notion/new-source-id",
            name="new item",
            category="alpha",
            learnings="new",
            improvements="",
            created_time="",
        )
        old_record = dict(moved_record(), operation_id=workflow.operation_id("session-1", SOURCE_ID, 1))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            state_path = tmp_path / "state.json"
            self.write_json(config_path, {"source_database_id": SOURCE_DB, "target_databases": {"alpha": TARGET_DB}})
            self.write_json(
                state_path,
                {
                    "batch_id": 1,
                    "batch": [completed.to_state()],
                    "moved": [old_record],
                    "skipped": [],
                    "backup_session_id": "session-1",
                    "session_id": "session-1",
                },
            )
            args = argparse.Namespace(config=config_path, state=state_path, batch_size=None, move_limit=None, force=False)
            output = io.StringIO()

            with (
                patch.object(workflow, "preflight"),
                patch.object(workflow, "ensure_backup_session", return_value="session-1"),
                patch.object(workflow, "backup_active_batch"),
                patch.object(workflow, "make_batch", return_value=[new_candidate]),
                contextlib.redirect_stdout(output),
            ):
                workflow.command_next(args)

            updated = workflow.load_json(state_path, {})

        self.assertEqual(updated["batch_id"], 2)
        self.assertEqual(updated["batch"][0]["source_page_id"], "new-source-id")
        self.assertIn("new item", output.getvalue())

    def test_remove_sources_rejects_forged_target_not_present_in_ledger(self):
        client = FakeClient([ledger_page(target_id=TARGET_ID)])
        state = {"moved": [moved_record(target_id="FAKE_CREATED_TARGET_PAGE_ID")]}

        updated = self.run_with_files(client, state, workflow.command_remove_sources)

        self.assertEqual(client.archive_calls, [])
        self.assertEqual(client.ledger_updates, [])
        self.assertEqual(len(updated["moved"]), 1)
        self.assertEqual(len(updated["needs_review"]), 1)

    def test_remove_sources_archives_source_and_updates_verified_ledger_row(self):
        client = FakeClient([ledger_page()])
        state = {"moved": [moved_record()]}

        updated = self.run_with_files(client, state, workflow.command_remove_sources)

        self.assertEqual(client.archive_calls, [SOURCE_ID])
        self.assertEqual(client.ledger_updates, [("ledger-page-id", "removed")])
        self.assertEqual(updated["moved"], [])
        self.assertEqual(len(updated["removed"]), 1)

    def test_remove_sources_rejects_unchecked_source_row(self):
        class UncheckedSourceClient(FakeClient):
            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == SOURCE_ID:
                    return {
                        "id": SOURCE_ID,
                        "archived": False,
                        "parent": {"type": "database_id", "database_id": SOURCE_DB},
                        "properties": {
                            "完成": {"type": "checkbox", "checkbox": False},
                        },
                    }
                if compact == TARGET_ID:
                    return page(TARGET_ID, TARGET_DB)
                return page(compact, "unexpected-database")

        client = UncheckedSourceClient([ledger_page()])
        state = {"moved": [moved_record()]}

        updated = self.run_with_files(client, state, workflow.command_remove_sources)

        self.assertEqual(client.archive_calls, [])
        self.assertEqual(client.ledger_updates, [])
        self.assertEqual(len(updated["moved"]), 1)
        self.assertEqual(len(updated["needs_review"]), 1)

    def test_remove_sources_keeps_pending_when_source_changed_after_move(self):
        class ChangedSourceClient(FakeClient):
            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == SOURCE_ID:
                    return source_row(SOURCE_ID, name="changed item", learnings="new")
                if compact == TARGET_ID:
                    return page(TARGET_ID, TARGET_DB)
                return page(compact, "unexpected-database")

        client = ChangedSourceClient([ledger_page()])
        state = {"moved": [moved_record()]}

        updated = self.run_with_files(client, state, workflow.command_remove_sources)

        self.assertEqual(client.archive_calls, [])
        self.assertEqual(client.ledger_updates, [])
        self.assertEqual(len(updated["moved"]), 1)
        self.assertEqual(len(updated["needs_review"]), 1)
        self.assertIn("current source content is not saved", updated["needs_review"][0]["needs_review"])

    def test_confirm_finalizes_manual_match_when_current_content_is_saved(self):
        client = FakeClient([ledger_page(action="manual-match")])
        state = {"manual_matches": [dict(moved_record(), action="manual-match")], "awaiting_done_confirm": True}

        updated = self.run_with_files(client, state, workflow.command_confirm)

        self.assertEqual(client.archive_calls, [SOURCE_ID])
        self.assertEqual(client.ledger_updates, [("ledger-page-id", "removed")])
        self.assertEqual(updated["manual_matches"], [])
        self.assertEqual(len(updated["removed"]), 1)

    def test_remove_sources_can_finalize_ledger_pending_when_local_state_is_empty(self):
        client = FakeClient([ledger_page()])
        state = {"moved": []}

        updated = self.run_with_files(client, state, workflow.command_remove_sources)

        self.assertEqual(client.archive_calls, [SOURCE_ID])
        self.assertEqual(client.ledger_updates, [("ledger-page-id", "removed")])
        self.assertEqual(len(updated["removed"]), 1)
        self.assertEqual(updated["removed"][0]["source_page_id"], SOURCE_ID)

    def test_remove_sources_does_not_finalize_unchecked_ledger_pending(self):
        class UncheckedPendingClient(FakeClient):
            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == SOURCE_ID:
                    return {
                        "id": SOURCE_ID,
                        "archived": False,
                        "parent": {"type": "database_id", "database_id": SOURCE_DB},
                        "properties": {
                            "完成": {"type": "checkbox", "checkbox": False},
                        },
                    }
                if compact == TARGET_ID:
                    return page(TARGET_ID, TARGET_DB)
                return page(compact, "unexpected-database")

        client = UncheckedPendingClient([ledger_page()])
        state = {"moved": []}

        updated = self.run_with_files(client, state, workflow.command_remove_sources)

        self.assertEqual(client.archive_calls, [])
        self.assertEqual(client.ledger_updates, [])
        self.assertEqual(updated.get("removed"), None)

    def test_remove_sources_marks_ledger_pending_needs_review_when_target_moved(self):
        class TargetMovedClient(FakeClient):
            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == SOURCE_ID:
                    return page(SOURCE_ID, SOURCE_DB)
                if compact == TARGET_ID:
                    return page(TARGET_ID, "unexpected-database")
                return page(compact, "unexpected-database")

        client = TargetMovedClient([ledger_page()])
        state = {"moved": []}

        updated = self.run_with_files(client, state, workflow.command_remove_sources)

        self.assertEqual(client.archive_calls, [])
        self.assertEqual(client.ledger_updates, [])
        self.assertEqual(len(updated["needs_review"]), 1)

    def test_remove_sources_skips_ledger_pending_when_source_already_archived(self):
        class ArchivedSourceClient(FakeClient):
            def retrieve_page(self, page_id):
                compact = workflow.notion_id(page_id)
                if compact == SOURCE_ID:
                    archived = page(SOURCE_ID, SOURCE_DB)
                    archived["archived"] = True
                    return archived
                if compact == TARGET_ID:
                    return page(TARGET_ID, TARGET_DB)
                return page(compact, "unexpected-database")

        client = ArchivedSourceClient([ledger_page()])
        state = {"moved": []}

        updated = self.run_with_files(client, state, workflow.command_remove_sources)

        self.assertEqual(client.archive_calls, [])
        self.assertEqual(client.ledger_updates, [])
        self.assertEqual(updated.get("removed"), None)


if __name__ == "__main__":
    unittest.main()
