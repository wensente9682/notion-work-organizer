import copy
import json
import pickle
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from new_system_approval import ApprovalLedger
from new_system_connector_action_adapter import CanonicalConnectorActionAdapter
from new_system_onboarding import (
    begin, expected_state, ingest_execution, ingest_source_dispatch,
    ingest_journal_source_dispatch, next_action, prepare_journal_source_dispatch,
    prepare_source_dispatch, preview, source_dispatch_request,
)
from new_system_recovery_journal import RecoveryJournal
from new_system_write_coordinator import WriteCoordinator


class NewSystemOnboardingTests(unittest.TestCase):
    def test_confirmed_input_produces_one_deterministic_first_action(self):
        state = begin("blank-page", ["Research", "Writing"])

        self.assertEqual("pre-source", state.phase)
        self.assertEqual(("Research", "Writing"), state.categories)
        self.assertEqual(next_action(state), next_action(begin("blank-page", ["Research", "Writing"])))

    def test_backend_owns_exact_schema_and_connector_payload(self):
        action = next_action(begin("blank-page", ["Research", "Writing"]))

        self.assertEqual("create_database", action["kind"])
        self.assertEqual({"page_id": "blank-page"}, action["target"])
        request = action["payload"]["request"]
        self.assertEqual("Daily Work", request["title"])
        self.assertIn('"Category" SELECT(\'Research\', \'Writing\')', request["schema"])
        self.assertIn('"Time Blocks" RICH_TEXT', request["schema"])

    def test_preview_is_plain_language_and_action_enters_approval(self):
        action = next_action(begin("blank-page", ["Research", "Writing"]))
        text = preview(begin("blank-page", ["Research", "Writing"]), action)

        self.assertIn("Daily Work", text)
        self.assertIn("Research, Writing", text)
        self.assertIn("Task, Done, Category, Takeaway, Improvement, Work Date, Time Blocks", text)
        self.assertNotIn("CREATE TABLE", text)
        ledger = ApprovalLedger()
        self.assertEqual("preview-ready", ledger.preview(action, {"blank": True}).to_public_dict()["status"])

    def test_preview_preserves_confirmed_category_characters(self):
        state = begin("blank-page", ["Reader's Notes", "Plan) Review", "A, B"])

        text = preview(state, next_action(state))

        self.assertIn("Reader's Notes, Plan) Review, A, B", text)

    def test_categories_are_ordered_unique_nonempty_text(self):
        with self.assertRaises(ValueError):
            begin("blank-page", ["Research", "Research"])
        with self.assertRaises(ValueError):
            begin("blank-page", [""])

    def test_manual_ingest_bypass_is_not_public(self):
        import new_system_onboarding
        import new_system_write_coordinator
        for name in ("ingest_result", "_ingest_result", "_MINT_AUTH", "_mint_source_execution", "_SourceExecution"):
            self.assertFalse(hasattr(new_system_onboarding, name))
        for name in ("_MINT_AUTH", "_mint_source_execution", "_source_mint"):
            self.assertFalse(hasattr(new_system_write_coordinator, name))
            self.assertFalse(hasattr(WriteCoordinator, name))

    def test_action_is_unchanged_through_approval_coordinator_adapter_and_replay_writes_once(self):
        state, action, expected = begin("blank-page", ["Research"]), None, {"blank": True}
        action = next_action(state)
        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(action, expected), accepted=True)

        class Connector:
            def __init__(self): self.writes = 0
            def read_canonical(self, request): return expected
            def write_canonical(self, request):
                self.writes += 1
                return {"status": "success", "database_id": "db-1", "data_source_id": "ds-1"}

        connector = Connector()
        first = WriteCoordinator(ledger).run(envelope, action, expected, CanonicalConnectorActionAdapter(connector))
        second = WriteCoordinator(ledger).run(envelope, action, expected, CanonicalConnectorActionAdapter(connector))
        self.assertEqual("source-created", ingest_execution(state, action, first).phase)
        self.assertEqual("write-unresolved", ingest_execution(state, action, second).phase)
        self.assertEqual(1, connector.writes)

    def test_bound_connector_response_ingests_once(self):
        state, action = begin("blank-page", ["Research"]), None
        action = next_action(state)
        expected = expected_state(state, action)
        ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
        class Connector:
            def read_canonical(self, request): return expected
            def write_canonical(self, request): return {"status": "success", "database_id": "db-1", "data_source_id": "ds-1"}
        execution = WriteCoordinator(ledger).run(envelope, action, expected, CanonicalConnectorActionAdapter(Connector()))
        created = ingest_execution(state, action, execution)
        self.assertEqual("source-created", created.phase)
        self.assertEqual("write-unresolved", ingest_execution(state, action, execution).phase)

    def test_invalid_identities_and_unresolved_outcomes_never_create_source(self):
        state, action = begin("blank-page", ["Research"]), None
        action = next_action(state)
        for database_id, data_source_id in ((" TODO", "ds"), ("db ", "ds"), ("db", "db"), ("", "ds")):
            class Execution:
                status = "completed"; outcome_certainty = "confirmed"; action_digest = "x"; attempt_fingerprint = "a"; reason = "completed"
                source_result = {"action_digest": "x", "attempt_fingerprint": "a", "response": {"database_id": database_id, "data_source_id": data_source_id}}
            self.assertEqual("write-unresolved", ingest_execution(state, action, Execution()).phase)

    def _execution(self, outcome):
        state = begin("blank-page", ["Research"])
        action = next_action(state)
        expected = expected_state(state, action)
        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(action, expected), accepted=True)

        class Connector:
            def __init__(self): self.writes = 0
            def read_canonical(self, request): return expected
            def write_canonical(self, request): self.writes += 1; return outcome

        connector = Connector()
        return state, action, WriteCoordinator(ledger).run(
            envelope, action, expected, CanonicalConnectorActionAdapter(connector)
        ), connector

    def test_source_terminal_outcomes_are_opaque_and_classified(self):
        cases = (
            ({"status": "success", "database_id": "db-1", "data_source_id": "ds-1"}, "source-created"),
            ({"status": "success", "database_id": "db-1"}, "write-unresolved"),
            ({"status": "success", "database_id": "UNKNOWN", "data_source_id": "ds-1"}, "write-unresolved"),
            ({"status": "success", "database_id": "db-1 ", "data_source_id": "ds-1"}, "write-unresolved"),
            ({"status": "success", "database_id": "db-1", "data_source_id": "db-1"}, "write-unresolved"),
            ({"status": "no-op"}, "write-failed"),
            ({"status": "changed"}, "write-failed"),
            ({"status": "partial"}, "write-unresolved"),
            ({"status": "unknown"}, "write-unresolved"),
        )
        execution_type = None
        for outcome, phase in cases:
            with self.subTest(outcome=outcome):
                state, action, execution, connector = self._execution(outcome)
                execution_type = type(execution) if execution_type is None else execution_type
                self.assertIs(type(execution), execution_type)
                self.assertEqual(phase, ingest_execution(state, action, execution).phase)
                self.assertEqual(1, connector.writes)

    def test_execution_is_opaque_and_wrong_first_attempt_does_not_consume_it(self):
        state, action, execution, _ = self._execution(
            {"status": "success", "database_id": "db-1", "data_source_id": "ds-1"}
        )
        self.assertFalse(hasattr(execution, "response"))
        self.assertFalse(hasattr(execution, "database_id"))
        with self.assertRaises(AttributeError):
            execution.response = {"database_id": "changed"}
        with self.assertRaises(TypeError): copy.copy(execution)
        with self.assertRaises(TypeError): copy.deepcopy(execution)
        with self.assertRaises(TypeError): pickle.dumps(execution)
        wrong_state = type(state)(state.target_page_id, state.categories, "write-unresolved")
        self.assertEqual("write-unresolved", ingest_execution(wrong_state, action, execution).phase)
        wrong_action = dict(action); wrong_action["target"] = {"page_id": "other"}
        self.assertEqual("write-unresolved", ingest_execution(state, wrong_action, execution).phase)
        self.assertEqual("source-created", ingest_execution(state, action, execution).phase)
        self.assertEqual("write-unresolved", ingest_execution(state, action, execution).phase)

    def test_handcrafted_mapping_or_duck_object_cannot_transition_state(self):
        state, action = begin("blank-page", ["Research"]), None
        action = next_action(state)
        forged = {
            "status": "completed", "outcome_certainty": "confirmed",
            "action_digest": "x", "response": {"status": "success", "database_id": "db", "data_source_id": "ds"},
        }
        class Duck: pass
        for value in (forged, Duck()):
            self.assertEqual("write-unresolved", ingest_execution(state, action, value).phase)

    def test_approved_matching_observation_prepares_one_exact_dispatch_without_connector_io(self):
        state = begin("blank-page", ["Study", "Personal"])
        action = next_action(state)
        expected = expected_state(state, action)
        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(action, expected), accepted=True)

        dispatch = prepare_source_dispatch(ledger, envelope, state, action, expected)

        self.assertIsNotNone(dispatch)
        self.assertEqual(action["payload"]["request"], source_dispatch_request(dispatch))
        self.assertIsNone(source_dispatch_request(dispatch))
        self.assertEqual("source-created", ingest_source_dispatch(
            state, action, dispatch,
            {"status": "success", "database_id": "db-1", "data_source_id": "ds-1"},
        ).phase)
        self.assertIsNone(prepare_source_dispatch(ledger, envelope, state, action, expected))

    def test_prepare_rejects_unapproved_drift_and_mismatched_action(self):
        state = begin("blank-page", ["Study"])
        action = next_action(state)
        expected = expected_state(state, action)
        ledger = ApprovalLedger()
        self.assertIsNone(prepare_source_dispatch(ledger, object(), state, action, expected))
        envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
        self.assertIsNone(prepare_source_dispatch(ledger, envelope, state, action, {"blank": False, "parent": "blank-page"}))
        state2 = begin("second-page", ["Study"])
        action2 = next_action(state2)
        envelope2 = ledger.issue(ledger.preview(action2, expected_state(state2, action2)), accepted=True)
        changed = dict(action2); changed["target"] = {"page_id": "other"}
        self.assertIsNone(prepare_source_dispatch(ledger, envelope2, state2, changed, expected_state(state2, action2)))

    def test_raw_dispatch_results_are_classified_once_without_connector(self):
        cases = (
            ({"status": "success", "database_id": "db-1", "data_source_id": "ds-1"}, "source-created"),
            ({"status": "no-op"}, "write-failed"),
            ({"status": "failure"}, "write-failed"),
            ({"status": "partial"}, "write-unresolved"),
            ({"status": "ambiguous"}, "write-unresolved"),
            ({"status": "unknown"}, "write-unresolved"),
            ({"status": "success", "database_id": "db-1"}, "write-unresolved"),
        )
        for raw, phase in cases:
            with self.subTest(raw=raw):
                state = begin("blank-page", ["Study"])
                action = next_action(state)
                expected = expected_state(state, action)
                ledger = ApprovalLedger()
                envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
                dispatch = prepare_source_dispatch(ledger, envelope, state, action, expected)
                source_dispatch_request(dispatch)
                result = ingest_source_dispatch(state, action, dispatch, raw)
                self.assertEqual(phase, result.phase)
                self.assertEqual("write-unresolved", ingest_source_dispatch(state, action, dispatch, raw).phase)

    def test_wrong_action_does_not_consume_dispatch(self):
        state = begin("blank-page", ["Study"])
        action = next_action(state)
        expected = expected_state(state, action)
        ledger = ApprovalLedger()
        dispatch = prepare_source_dispatch(ledger, ledger.issue(ledger.preview(action, expected), accepted=True), state, action, expected)
        source_dispatch_request(dispatch)
        wrong = dict(action); wrong["target"] = {"page_id": "other"}
        raw = {"status": "success", "database_id": "db-1", "data_source_id": "ds-1"}
        self.assertEqual("write-unresolved", ingest_source_dispatch(state, wrong, dispatch, raw).phase)
        self.assertEqual("source-created", ingest_source_dispatch(state, action, dispatch, raw).phase)

    def test_unclaimed_dispatch_and_fake_ledger_cannot_progress(self):
        state = begin("blank-page", ["Study"])
        action = next_action(state)
        expected = expected_state(state, action)
        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
        dispatch = prepare_source_dispatch(ledger, envelope, state, action, expected)
        raw = {"status": "success", "database_id": "db-1", "data_source_id": "ds-1"}
        self.assertEqual("write-unresolved", ingest_source_dispatch(state, action, dispatch, raw).phase)

        class FakeLedger:
            def claim_for_write(self, _): raise AssertionError("must not be called")
        self.assertIsNone(prepare_source_dispatch(FakeLedger(), envelope, state, action, expected))

    def test_concurrent_request_claim_has_one_winner(self):
        state = begin("blank-page", ["Study"])
        action = next_action(state)
        expected = expected_state(state, action)
        ledger = ApprovalLedger()
        dispatch = prepare_source_dispatch(ledger, ledger.issue(ledger.preview(action, expected), accepted=True), state, action, expected)
        results = []
        threads = [threading.Thread(target=lambda: results.append(source_dispatch_request(dispatch))) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(1, sum(value is not None for value in results))

    def test_journal_dispatch_round_trips_across_processes_once(self):
        with tempfile.TemporaryDirectory() as root:
            script = """
import json, sys
from new_system_approval import ApprovalLedger
from new_system_onboarding import begin, expected_state, next_action, prepare_journal_source_dispatch
from new_system_recovery_journal import RecoveryJournal
state = begin('blank-page', ['Study', 'Personal']); action = next_action(state); expected = expected_state(state, action)
ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
dispatch = prepare_journal_source_dispatch(ledger, envelope, state, action, expected, RecoveryJournal(sys.argv[1]))
print(json.dumps({'digest': dispatch.action_digest, 'attempt': dispatch.attempt_fingerprint, 'request_json': dispatch.request_json}))
"""
            prepared = json.loads(subprocess.check_output([sys.executable, "-c", script, root], text=True))
            state = begin("blank-page", ["Study", "Personal"])
            action = next_action(state)
            result = ingest_journal_source_dispatch(
                state, action, RecoveryJournal(root), prepared["digest"], prepared["attempt"],
                {"status": "success", "database_id": "db-1", "data_source_id": "ds-1"},
            )
            request = json.loads(prepared["request_json"])
            self.assertEqual(action["payload"]["request"], request)
            self.assertEqual(prepared["request_json"], json.dumps(
                action["payload"]["request"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ))
            self.assertEqual("source-created", result.phase)
            self.assertEqual("write-unresolved", ingest_journal_source_dispatch(
                state, action, RecoveryJournal(root), prepared["digest"], prepared["attempt"],
                {"status": "success", "database_id": "db-1", "data_source_id": "ds-1"},
            ).phase)

    def test_journal_dispatch_rejects_wrong_identifiers_and_storage_failure(self):
        with tempfile.TemporaryDirectory() as root:
            state = begin("blank-page", ["Study"]); action = next_action(state); expected = expected_state(state, action)
            ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
            prepared = prepare_journal_source_dispatch(ledger, envelope, state, action, expected, RecoveryJournal(root))
            self.assertIsNotNone(prepared)
            self.assertIsNone(prepare_journal_source_dispatch(ledger, envelope, state, action, expected, RecoveryJournal(root)))
            self.assertEqual("write-unresolved", ingest_journal_source_dispatch(
                state, action, RecoveryJournal(root), "0" * 64, prepared.attempt_fingerprint, {"status": "unknown"}
            ).phase)
            self.assertEqual("write-unresolved", ingest_journal_source_dispatch(
                state, action, RecoveryJournal(root), prepared.action_digest, "0" * 32, {"status": "unknown"}
            ).phase)
            self.assertEqual("write-unresolved", ingest_journal_source_dispatch(
                state, action, RecoveryJournal(root), prepared.action_digest, prepared.attempt_fingerprint, {"status": "ambiguous"}
            ).phase)
            self.assertEqual("write-unresolved", ingest_journal_source_dispatch(
                state, action, RecoveryJournal(root), prepared.action_digest, prepared.attempt_fingerprint, {"status": "ambiguous"}
            ).phase)
        with tempfile.TemporaryDirectory() as root:
            state = begin("blank-page", ["Study"]); action = next_action(state); expected = expected_state(state, action)
            ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
            journal = RecoveryJournal(root)
            with mock.patch.object(RecoveryJournal, "transition", return_value=type("R", (), {"to_public_dict": lambda self: {"status": "rejected"}})()):
                self.assertIsNone(prepare_journal_source_dispatch(ledger, envelope, state, action, expected, journal))

    def test_hosted_create_database_result_is_normalized_by_backend(self):
        parent = "a" * 32
        database = "b" * 32
        source = "12345678-1234-1234-1234-123456789abc"
        raw = {
            "result": (
                f'Created database: &lt;database url="{{{{https://app.notion.com/p/{database}}}}}"&gt;'
                f'The title of this Database is: Daily Work. &lt;parent-page url="https://app.notion.com/p/{parent}" /&gt; '
                f'&lt;data-source url="{{{{collection://{source}}}}}"&gt;&lt;/data-source&gt; &lt;data-source-state&gt;ready&lt;/data-source-state&gt;'
                f'&lt;/database&gt;'
            )
        }
        def prepared(root):
            state = begin(parent, ["Study"]); action = next_action(state); expected = expected_state(state, action)
            ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
            dispatch = prepare_journal_source_dispatch(ledger, envelope, state, action, expected, RecoveryJournal(root))
            return state, action, dispatch
        with tempfile.TemporaryDirectory() as root:
            state, action, dispatch = prepared(root)
            result = ingest_journal_source_dispatch(state, action, RecoveryJournal(root), dispatch.action_digest, dispatch.attempt_fingerprint, raw)
            self.assertEqual(("source-created", database, source), (result.phase, result.source_database_id, result.source_data_source_id))
        for changed in (
            raw["result"].replace(parent, "c" * 32),
            raw["result"].replace("Daily Work", "Other Work"),
            raw["result"] + f' &lt;data-source url="{{{{collection://{source}}}}}"&gt;',
            raw["result"] + f' &lt;database url="{{{{https://app.notion.com/p/{"c" * 32}}}}}"&gt;&lt;/database&gt;',
            "Created database: malformed",
        ):
            with self.subTest(changed=changed[:40]), tempfile.TemporaryDirectory() as root:
                state, action, dispatch = prepared(root)
                result = ingest_journal_source_dispatch(state, action, RecoveryJournal(root), dispatch.action_digest, dispatch.attempt_fingerprint, {"result": changed})
                self.assertEqual("write-unresolved", result.phase)

    def test_database_result_binding_rejects_cross_block_evidence(self):
        parent, database = "a" * 32, "b" * 32
        source = "12345678-1234-1234-1234-123456789abc"
        valid = (
            f'Created database: &lt;database url="{{{{https://app.notion.com/p/{database}}}}}"&gt;'
            f'The title of this Database is: Daily Work. &lt;parent-page url="https://app.notion.com/p/{parent}" /&gt; '
            f'&lt;data-source url="{{{{collection://{source}}}}}"&gt;&lt;/data-source&gt;&lt;/database&gt;'
        )
        invalid = (
            valid.replace("&lt;/database&gt;", ""),
            valid.replace("Daily Work", "Other Work") + " Daily Work",
            valid.replace("Daily Work. &lt;parent-page", "Other Work. &lt;parent-page") + " The title of this Database is: Daily Work.",
            valid + valid,
        )
        for text in invalid:
            with self.subTest(text=text[:40]), tempfile.TemporaryDirectory() as root:
                state = begin(parent, ["Study"]); action = next_action(state); expected = expected_state(state, action)
                ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
                dispatch = prepare_journal_source_dispatch(ledger, envelope, state, action, expected, RecoveryJournal(root))
                result = ingest_journal_source_dispatch(state, action, RecoveryJournal(root), dispatch.action_digest, dispatch.attempt_fingerprint, {"result": text})
                self.assertEqual("write-unresolved", result.phase)

    def test_global_braced_identity_token_injection_is_rejected(self):
        parent, database = "a" * 32, "b" * 32
        other_database = "c" * 32
        source, other_source = "12345678-1234-1234-1234-123456789abc", "87654321-4321-4321-4321-cba987654321"
        valid = (
            f'Created database: &lt;database url="{{{{https://app.notion.com/p/{database}}}}}"&gt;'
            f'The title of this Database is: Daily Work. &lt;parent-page url="https://app.notion.com/p/{parent}" /&gt; '
            f'&lt;data-source url="{{{{collection://{source}}}}}"&gt;&lt;/data-source&gt;&lt;/database&gt;'
        )
        injections = (
            f' {{{{https://app.notion.com/p/{database}}}}}',
            f' {{{{https://app.notion.com/p/{other_database}}}}}',
            f' {{{{collection://{source}}}}}',
            f' {{{{collection://{other_source}}}}}',
        )
        cases = tuple(valid.replace("&lt;/database&gt;", item + "&lt;/database&gt;") for item in injections) + tuple(valid + item for item in injections)
        self.assertEqual(8, len(cases))
        for text in cases:
            with self.subTest(text=text[-60:]), tempfile.TemporaryDirectory() as root:
                state = begin(parent, ["Study"]); action = next_action(state); expected = expected_state(state, action)
                ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action, expected), accepted=True)
                dispatch = prepare_journal_source_dispatch(ledger, envelope, state, action, expected, RecoveryJournal(root))
                self.assertEqual("write-unresolved", ingest_journal_source_dispatch(
                    state, action, RecoveryJournal(root), dispatch.action_digest, dispatch.attempt_fingerprint, {"result": text}
                ).phase)


if __name__ == "__main__":
    unittest.main()
