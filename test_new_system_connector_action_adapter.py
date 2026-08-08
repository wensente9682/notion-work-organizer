"""T7 synthetic-only red gate for the canonical connector action adapter."""

import copy
import json
import threading
import unittest

from new_system_approval import ApprovalEnvelope, ApprovalLedger
from new_system_write_coordinator import ReadFailure, WriteCoordinator


CANARY = "synthetic-private-marker"

def action(kind="create_database"):
    return {
        "kind": kind,
        "target": {"parent": "synthetic-parent"},
        "payload": {"schema": {"title": "synthetic"}},
    }


def state(value="absent"):
    return {"target": {"parent": "synthetic-parent"}, "state": value}


def plain_tree_utf8_bytes(value):
    if type(value) is str:
        return len(value.encode("utf-8"))
    if type(value) is list:
        return sum(plain_tree_utf8_bytes(item) for item in value)
    if type(value) is dict:
        return sum(
            len(key.encode("utf-8")) + plain_tree_utf8_bytes(item)
            for key, item in value.items()
        )
    return 0


def alternate_action():
    return {
        "kind": "create_archive_container",
        "target": {"parent": "synthetic-parent"},
        "payload": {"title": "alternate"},
    }


def archive_action(display_name="Synthetic Archive"):
    return {
        "kind": "create_archive_target",
        "target": {"container": "synthetic-container", "category": "slot-1"},
        "payload": {
            "display_name": display_name,
            "schema": {"Task": "title"},
        },
    }


class DynamicTrap:
    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, _name):
        self.calls.append("getattr")
        raise RuntimeError(CANARY)

    def __repr__(self):
        self.calls.append("repr")
        raise RuntimeError(CANARY)


class SyntheticConnector:
    """A local double; no network or Notion implementation is present."""

    def __init__(self, read=None, write="success"):
        self.read_value = state() if read is None else read
        self.write_value = write
        self.read_calls = 0
        self.write_calls = 0

    def read_canonical(self, _request):
        self.read_calls += 1
        if isinstance(self.read_value, BaseException):
            raise self.read_value
        return self.read_value

    def write_canonical(self, _request):
        self.write_calls += 1
        if isinstance(self.write_value, BaseException):
            raise self.write_value
        return self.write_value


def adapter(connector):
    # Deliberately unresolved in Phase A: every test must be red without T7 code.
    from new_system_connector_action_adapter import CanonicalConnectorActionAdapter

    return CanonicalConnectorActionAdapter(connector)


def approved(ledger, current_action=None, expected=None):
    current_action = action() if current_action is None else current_action
    expected = state() if expected is None else expected
    return ledger.issue(ledger.preview(current_action, expected), accepted=True)


class CanonicalConnectorActionAdapterRedGate(unittest.TestCase):
    def test_archive_display_name_is_required_and_maps_to_create_database_title(self):
        class CapturingConnector(SyntheticConnector):
            def read_canonical(self, request):
                self.read_request = request
                return super().read_canonical(request)

            def write_canonical(self, request):
                self.write_request = request
                return super().write_canonical(request)

        connector = CapturingConnector()
        subject = adapter(connector)
        self.assertNotIsInstance(subject.read_exact(archive_action()), ReadFailure)
        self.assertEqual(
            {
                "parent": {"page_id": "synthetic-container"},
                "title": "Synthetic Archive",
                "schema": {"Task": "title"},
            },
            connector.read_request,
        )
        self.assertEqual("success", subject.write_once(archive_action()))
        self.assertEqual(connector.read_request, connector.write_request)

        connector = SyntheticConnector()
        missing = archive_action()
        del missing["payload"]["display_name"]
        self.assertIsInstance(adapter(connector).read_exact(missing), ReadFailure)
        self.assertEqual((0, 0), (connector.read_calls, connector.write_calls))

    def test_closed_allowlist_rejects_unknown_action_without_connector_call(self):
        connector = SyntheticConnector()
        result = adapter(connector).read_exact(action("existing-list.archive"))
        self.assertIsInstance(result, ReadFailure)
        self.assertEqual(0, connector.read_calls)
        self.assertEqual(0, connector.write_calls)

    def test_each_canonical_action_family_has_an_allowed_path_and_schema_mismatch_rejection(self):
        cases = (
            ("create_database", {"parent": "synthetic-parent"}, {"schema": {"title": "synthetic"}}),
            ("configure_property", {"database": "synthetic", "property": "Category"}, {"type": "select"}),
            ("configure_view_sort", {"database": "synthetic", "view": "ordered"}, {"sort": "Work Date DESC"}),
            ("create_archive_container", {"parent": "synthetic-parent"}, {"title": "archive"}),
            ("create_archive_target", {"container": "synthetic-container", "category": "slot-1"}, {"display_name": "Synthetic Archive", "schema": {"title": "synthetic"}}),
        )
        for kind, target, payload in cases:
            with self.subTest(family=kind):
                connector = SyntheticConnector()
                candidate = {"kind": kind, "target": target, "payload": payload}
                accepted = adapter(connector).read_exact(candidate)
                self.assertNotIsInstance(accepted, ReadFailure)
                rejected = adapter(connector).read_exact({**candidate, "payload": {}})
                self.assertIsInstance(rejected, ReadFailure)

    def test_exact_action_target_payload_and_state_reject_dynamic_input_before_read(self):
        calls = []
        connector = SyntheticConnector()
        cases = (
            {"kind": "create_database", "target": {}, "payload": {"schema": {}}},
            {**action(), "extra": True},
            {**action(), "target": DynamicTrap(calls)},
            {**action(), "payload": {"schema": DynamicTrap(calls)}},
        )
        for candidate in cases:
            with self.subTest(case="schema"):
                result = adapter(connector).read_exact(candidate)
                self.assertIsInstance(result, ReadFailure)
                self.assertEqual(0, connector.read_calls)
                self.assertEqual([], calls)

    def test_archive_display_name_rejects_malformed_dynamic_and_utf8_over_limit_values(self):
        class TextSubclass(str):
            pass

        cases = (
            "",
            "   ",
            TextSubclass("Synthetic Archive"),
            DynamicTrap([]),
            None,
            1,
            "a" * 129,
            "界" * 43,
        )
        for display_name in cases:
            with self.subTest(value_type=type(display_name).__name__):
                connector = SyntheticConnector()
                result = adapter(connector).read_exact(archive_action(display_name))
                self.assertIsInstance(result, ReadFailure)
                self.assertEqual((0, 0), (connector.read_calls, connector.write_calls))

        connector = SyntheticConnector()
        self.assertNotIsInstance(
            adapter(connector).read_exact(archive_action("a" * 128)),
            ReadFailure,
        )
        self.assertEqual(1, connector.read_calls)

    def test_archive_display_name_tamper_revokes_write_and_public_evidence_is_sanitized(self):
        private_title = "private-synthetic-archive-title"
        connector = SyntheticConnector()
        subject = adapter(connector)
        self.assertNotIsInstance(
            subject.read_exact(archive_action(private_title)), ReadFailure
        )
        outcome = subject.write_once(archive_action(private_title + "-changed"))
        retry = subject.write_once(archive_action(private_title))
        public = json.dumps(
            {
                "outcome": outcome,
                "retry": retry,
            },
            sort_keys=True,
        )
        self.assertNotEqual("success", outcome)
        self.assertNotEqual("success", retry)
        self.assertEqual((1, 0), (connector.read_calls, connector.write_calls))
        self.assertNotIn(private_title, public)

    def test_missing_forged_and_reused_approval_have_correct_lifecycle_facts(self):
        connector = SyntheticConnector()
        subject = adapter(connector)
        coordinator = WriteCoordinator(ApprovalLedger())
        missing = coordinator.run(None, action(), state(), subject)
        forged = coordinator.run(ApprovalEnvelope(), action(), state(), subject)
        self.assertFalse(missing.authorized)
        self.assertFalse(forged.authorized)
        self.assertEqual(0, connector.read_calls)
        self.assertEqual(0, connector.write_calls)
        ledger = ApprovalLedger()
        envelope = approved(ledger)
        first = WriteCoordinator(ledger).run(envelope, action(), state(), subject)
        reused = WriteCoordinator(ledger).run(envelope, action(), state(), subject)
        self.assertEqual("completed", first.status)
        self.assertTrue(first.approval_consumed)
        self.assertFalse(reused.authorized)
        self.assertEqual(1, connector.read_calls)
        self.assertEqual(1, connector.write_calls)

    def test_read_then_drift_consumes_approval_without_write(self):
        connector = SyntheticConnector(read=state("changed"))
        ledger = ApprovalLedger()
        result = WriteCoordinator(ledger).run(approved(ledger), action(), state(), adapter(connector))
        self.assertEqual("recovery-required", result.status)
        self.assertTrue(result.approval_consumed)
        self.assertEqual(1, connector.read_calls)
        self.assertEqual(0, connector.write_calls)

    def test_reentrant_and_cross_instance_attempts_have_one_read_write_path(self):
        entered, release = threading.Event(), threading.Event()
        connector = SyntheticConnector()
        ledger = ApprovalLedger()
        envelope = approved(ledger)
        first_result = []

        class BlockingConnector(SyntheticConnector):
            def read_canonical(self, request):
                self.read_calls += 1
                entered.set()
                release.wait(1)
                return self.read_value

        connector = BlockingConnector()
        subject = adapter(connector)
        thread = threading.Thread(target=lambda: first_result.append(
            WriteCoordinator(ledger).run(envelope, action(), state(), subject)
        ))
        thread.start()
        self.assertTrue(entered.wait(1))
        second = WriteCoordinator(ledger).run(envelope, action(), state(), subject)
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual("attempt-in-progress", second.status)
        self.assertEqual(1, connector.read_calls)
        self.assertLessEqual(connector.write_calls, 1)

    def test_connector_read_reentry_cannot_start_a_second_attempt(self):
        ledger = ApprovalLedger()
        envelope = approved(ledger)
        nested = []

        class ReentrantConnector(SyntheticConnector):
            def read_canonical(self, _request):
                self.read_calls += 1
                nested.append(WriteCoordinator(ledger).run(
                    envelope, action(), state(), self.subject
                ))
                return self.read_value

        connector = ReentrantConnector()
        connector.subject = adapter(connector)
        outer = WriteCoordinator(ledger).run(
            envelope, action(), state(), connector.subject
        )
        self.assertEqual("attempt-in-progress", nested[0].status)
        self.assertLessEqual(connector.read_calls, 1)
        self.assertLessEqual(connector.write_calls, 1)
        self.assertIn(outer.status, {"completed", "recovery-required"})

    def test_malicious_response_or_exception_is_static_and_has_no_protocol_leak(self):
        calls = []
        connector = SyntheticConnector(read=DynamicTrap(calls))
        result = adapter(connector).read_exact(action())
        public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True)
        self.assertIsInstance(result, ReadFailure)
        self.assertEqual([], calls)
        self.assertNotIn(CANARY, public)

    def test_resource_limits_and_no_progress_stop_after_one_read(self):
        for response in ({"items": list(range(17))}, {"kind": "no-progress"}):
            with self.subTest(response="bounded"):
                connector = SyntheticConnector(read=response)
                result = adapter(connector).read_exact(action())
                self.assertIsInstance(result, ReadFailure)
                self.assertEqual(1, connector.read_calls)
                self.assertEqual(0, connector.write_calls)

    def test_read_and_write_phase_exceptions_stop_without_retry(self):
        connector = SyntheticConnector(read=RuntimeError(CANARY))
        result = adapter(connector).read_exact(action())
        self.assertIsInstance(result, ReadFailure)
        self.assertEqual(1, connector.read_calls)
        self.assertEqual(0, connector.write_calls)
        connector = SyntheticConnector(write=RuntimeError(CANARY))
        ledger = ApprovalLedger()
        result = WriteCoordinator(ledger).run(approved(ledger), action(), state(), adapter(connector))
        public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True)
        self.assertEqual("recovery-required", result.status)
        self.assertEqual(1, connector.read_calls)
        self.assertEqual(1, connector.write_calls)
        self.assertNotIn(CANARY, public)

    def test_partial_and_ambiguous_write_request_are_each_single_attempt(self):
        for outcome in ("partial", "ambiguous"):
            with self.subTest(outcome=outcome):
                connector = SyntheticConnector(write=outcome)
                ledger = ApprovalLedger()
                result = WriteCoordinator(ledger).run(approved(ledger), action(), state(), adapter(connector))
                self.assertEqual(1, connector.write_calls)
                self.assertNotEqual("completed", result.status)

    def test_public_evidence_is_immutable_truthful_and_sanitized(self):
        connector = SyntheticConnector(read=state(CANARY))
        result = adapter(connector).read_exact(action())
        public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True)
        self.assertNotIn(CANARY, public)
        with self.assertRaises(TypeError):
            result.evidence["phase"] = "completed"

    def test_write_once_rejects_without_a_completed_exact_read(self):
        connector = SyntheticConnector()
        result = adapter(connector).write_once(action())
        self.assertNotEqual("success", result)
        self.assertEqual(0, connector.write_calls)

    def test_failed_read_is_terminal_and_cannot_retry_or_write(self):
        for response in (RuntimeError(CANARY), {}, {"kind": "no-progress"}, {"items": list(range(17))}):
            with self.subTest(response=type(response).__name__):
                connector = SyntheticConnector(read=response)
                subject = adapter(connector)
                first = subject.read_exact(action())
                connector.read_value = state()
                second = subject.read_exact(action())
                write = subject.write_once(action())
                self.assertIsInstance(first, ReadFailure)
                self.assertIsInstance(second, ReadFailure)
                self.assertNotEqual("success", write)
                self.assertEqual(1, connector.read_calls)
                self.assertEqual(0, connector.write_calls)

    def test_empty_response_is_not_a_completed_exact_read_or_write_authority(self):
        connector = SyntheticConnector(read={})
        subject = adapter(connector)
        result = subject.read_exact(action())
        write = subject.write_once(action())
        self.assertIsInstance(result, ReadFailure)
        self.assertNotEqual("success", write)
        self.assertEqual(1, connector.read_calls)
        self.assertEqual(0, connector.write_calls)

    def test_adapter_callback_reentry_is_static_and_never_deadlocks(self):
        for callback in ("read", "write"):
            with self.subTest(callback=callback):
                nested = []
                if callback == "read":
                    class CallbackConnector(SyntheticConnector):
                        def read_canonical(self, _request):
                            self.read_calls += 1
                            nested.append(self.subject.write_once(action()))
                            return state()
                else:
                    class CallbackConnector(SyntheticConnector):
                        def write_canonical(self, _request):
                            self.write_calls += 1
                            nested.append(self.subject.write_once(action()))
                            return "success"
                connector = CallbackConnector()
                subject = adapter(connector)
                connector.subject = subject
                if callback == "write":
                    self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
                result = []
                thread = threading.Thread(target=lambda: result.append(
                    subject.read_exact(action()) if callback == "read" else subject.write_once(action())
                ), daemon=True)
                thread.start()
                thread.join(0.2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(1, len(nested))
                self.assertNotEqual("success", nested[0])
                self.assertLessEqual(connector.read_calls, 1)
                self.assertLessEqual(connector.write_calls, 1)

    def test_failure_evidence_reports_completed_phase_and_actual_calls(self):
        invalid = adapter(SyntheticConnector()).read_exact({"kind": "unknown", "target": {}, "payload": {}})
        unavailable = adapter(object()).read_exact(action())
        for result in (invalid, unavailable):
            with self.subTest(result=result.reason):
                self.assertEqual("claim", result.evidence["phase"])
                self.assertFalse(result.evidence["read_attempted"])
                self.assertFalse(result.evidence["write_attempted"])
        failed = adapter(SyntheticConnector(read={})).read_exact(action())
        self.assertEqual("read", failed.evidence["phase"])
        self.assertTrue(failed.evidence["read_attempted"])
        self.assertFalse(failed.evidence["write_attempted"])

    def test_adapter_global_claim_blocks_a_second_action_after_failed_or_successful_read(self):
        for response in (RuntimeError(CANARY), state()):
            with self.subTest(response=type(response).__name__):
                connector = SyntheticConnector(read=response)
                subject = adapter(connector)
                first = subject.read_exact(action())
                connector.read_value = state()
                second = subject.read_exact(alternate_action())
                self.assertNotIsInstance(first, type(None))
                self.assertIsInstance(second, ReadFailure)
                self.assertEqual(1, connector.read_calls)
                self.assertEqual(0, connector.write_calls)

    def test_cross_action_concurrency_has_one_external_read(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingConnector(SyntheticConnector):
            def read_canonical(self, _request):
                self.read_calls += 1
                entered.set()
                release.wait(1)
                return state()
        connector = BlockingConnector()
        subject = adapter(connector)
        first = []
        thread = threading.Thread(target=lambda: first.append(subject.read_exact(action())))
        thread.start()
        self.assertTrue(entered.wait(1))
        second = subject.read_exact(alternate_action())
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(second, ReadFailure)
        self.assertEqual(1, connector.read_calls)

    def test_cross_action_read_callback_cannot_start_a_second_read(self):
        nested = []
        class CallbackConnector(SyntheticConnector):
            def read_canonical(self, _request):
                self.read_calls += 1
                nested.append(self.subject.read_exact(alternate_action()))
                return state()
        connector = CallbackConnector()
        subject = adapter(connector)
        connector.subject = subject
        subject.read_exact(action())
        self.assertIsInstance(nested[0], ReadFailure)
        self.assertEqual(1, connector.read_calls)

    def test_wrong_or_repeated_action_revokes_remaining_write_authority(self):
        connector = SyntheticConnector()
        subject = adapter(connector)
        self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
        self.assertNotEqual("success", subject.write_once(alternate_action()))
        self.assertNotEqual("success", subject.write_once(action()))
        self.assertEqual(0, connector.write_calls)

    def test_cross_action_write_callback_revokes_outer_write_authority(self):
        nested = []
        class CallbackConnector(SyntheticConnector):
            def write_canonical(self, _request):
                self.write_calls += 1
                nested.append(self.subject.write_once(alternate_action()))
                return "success"
        connector = CallbackConnector()
        subject = adapter(connector)
        connector.subject = subject
        self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
        self.assertNotEqual("success", subject.write_once(action()))
        self.assertNotEqual("success", nested[0])
        self.assertEqual(1, connector.write_calls)

    def test_inflight_read_callback_or_concurrent_violation_revokes_outer_authority(self):
        nested = []
        class CallbackConnector(SyntheticConnector):
            def read_canonical(self, _request):
                self.read_calls += 1
                nested.append(self.subject.write_once(alternate_action()))
                return state()
        connector = CallbackConnector()
        subject = adapter(connector)
        connector.subject = subject
        self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
        self.assertNotEqual("success", subject.write_once(action()))
        self.assertNotEqual("success", nested[0])

        entered, release = threading.Event(), threading.Event()
        class BlockingConnector(SyntheticConnector):
            def read_canonical(self, _request):
                self.read_calls += 1
                entered.set()
                release.wait(1)
                return state()
        connector = BlockingConnector()
        subject = adapter(connector)
        thread = threading.Thread(target=lambda: subject.read_exact(action()))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertIsInstance(subject.read_exact(alternate_action()), ReadFailure)
        release.set()
        thread.join(1)
        self.assertNotEqual("success", subject.write_once(action()))

    def test_any_malformed_unknown_or_existing_list_write_revokes_authority(self):
        for candidate in ({}, action("unknown"), action("existing-list.archive")):
            with self.subTest(candidate="invalid-write"):
                connector = SyntheticConnector()
                subject = adapter(connector)
                self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
                self.assertNotEqual("success", subject.write_once(candidate))
                self.assertNotEqual("success", subject.write_once(action()))
                self.assertEqual(0, connector.write_calls)

    def test_shallow_copy_cannot_create_second_read_or_write_authority(self):
        connector = SyntheticConnector()
        subject = adapter(connector)
        with self.assertRaises(TypeError):
            copy.copy(subject)
        self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
        with self.assertRaises(TypeError):
            copy.copy(subject)
        self.assertEqual("success", subject.write_once(action()))
        self.assertEqual(1, connector.read_calls)
        self.assertEqual(1, connector.write_calls)

    def test_inflight_callback_illegal_write_always_revokes_outer_authority(self):
        illegal_actions = (
            {},
            action("unknown"),
            action("existing-list.archive"),
            {**action(), "target": DynamicTrap([])},
        )
        for illegal in illegal_actions:
            with self.subTest(illegal="callback-write"):
                nested = []
                class CallbackConnector(SyntheticConnector):
                    def read_canonical(self, _request):
                        self.read_calls += 1
                        nested.append(self.subject.write_once(illegal))
                        return state()
                connector = CallbackConnector()
                subject = adapter(connector)
                connector.subject = subject
                self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
                self.assertNotEqual("success", nested[0])
                self.assertNotEqual("success", subject.write_once(action()))
                self.assertEqual(1, connector.read_calls)
                self.assertEqual(0, connector.write_calls)

    def test_illegal_read_always_revokes_inflight_or_completed_authority(self):
        illegal_actions = (
            {},
            action("unknown"),
            action("existing-list.archive"),
            {**action(), "target": DynamicTrap([])},
        )
        for illegal in illegal_actions:
            with self.subTest(illegal="inflight-read"):
                nested = []
                class CallbackConnector(SyntheticConnector):
                    def read_canonical(self, _request):
                        self.read_calls += 1
                        nested.append(self.subject.read_exact(illegal))
                        return state()
                connector = CallbackConnector()
                subject = adapter(connector)
                connector.subject = subject
                self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
                self.assertIsInstance(nested[0], ReadFailure)
                self.assertNotEqual("success", subject.write_once(action()))
                self.assertEqual(0, connector.write_calls)
            with self.subTest(illegal="completed-read"):
                connector = SyntheticConnector()
                subject = adapter(connector)
                self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
                self.assertIsInstance(subject.read_exact(illegal), ReadFailure)
                self.assertNotEqual("success", subject.write_once(action()))
                self.assertEqual(1, connector.read_calls)
                self.assertEqual(0, connector.write_calls)

    def test_write_callback_any_illegal_entry_revokes_outer_outcome(self):
        illegal_actions = (
            {},
            action("unknown"),
            action("existing-list.archive"),
            {**action(), "target": DynamicTrap([])},
        )
        for entry in ("read", "write"):
            for illegal in illegal_actions:
                with self.subTest(entry=entry, illegal="callback"):
                    nested = []
                    class CallbackConnector(SyntheticConnector):
                        def write_canonical(self, _request):
                            self.write_calls += 1
                            call = self.subject.read_exact if entry == "read" else self.subject.write_once
                            nested.append(call(illegal))
                            return "success"
                    connector = CallbackConnector()
                    subject = adapter(connector)
                    connector.subject = subject
                    self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
                    self.assertNotEqual("success", subject.write_once(action()))
                    self.assertEqual(1, connector.write_calls)
                    self.assertEqual(1, len(nested))

    def test_oversized_int_and_canonicalization_failure_are_static_before_connector(self):
        for number in (10 ** 5000, -(10 ** 5000)):
            with self.subTest(sign=number < 0):
                connector = SyntheticConnector()
                candidate = {**action(), "payload": {"schema": {"counter": number}}}
                result = adapter(connector).read_exact(candidate)
                self.assertIsInstance(result, ReadFailure)
                self.assertEqual(0, connector.read_calls)
                self.assertNotIn(CANARY, repr(result) + json.dumps(result.to_public_dict(), sort_keys=True))

    def test_utf8_scalar_and_total_plain_data_budgets_fail_closed(self):
        within = {**action(), "target": {"parent": "a" * 128}}
        connector = SyntheticConnector()
        self.assertNotIsInstance(adapter(connector).read_exact(within), ReadFailure)
        self.assertEqual(1, connector.read_calls)
        for text in ("a" * 129, "界" * 43):
            with self.subTest(kind="single", unicode=not text.isascii()):
                connector = SyntheticConnector()
                candidate = {**action(), "target": {"parent": text}}
                result = adapter(connector).read_exact(candidate)
                self.assertIsInstance(result, ReadFailure)
                self.assertEqual(0, connector.read_calls)
                self.assertNotEqual("success", adapter(connector).write_once(candidate))
        for part in ("a" * 90, "界" * 30):
            with self.subTest(kind="total", unicode=not part.isascii()):
                connector = SyntheticConnector()
                candidate = {
                    "kind": "configure_property",
                    "target": {"database": part, "property": part},
                    "payload": {"type": part},
                }
                self.assertIsInstance(adapter(connector).read_exact(candidate), ReadFailure)
                self.assertEqual(0, connector.read_calls)

    def test_request_plain_tree_utf8_budget_has_exact_256_257_boundary(self):
        within = {
            "kind": "configure_property",
            "target": {"database": "a" * 128, "property": "界" * 24},
            "payload": {"type": "x"},
        }
        over = {**within, "payload": {"type": "xx"}}
        self.assertEqual(256, plain_tree_utf8_bytes(within))
        self.assertEqual(257, plain_tree_utf8_bytes(over))

        connector = SyntheticConnector()
        self.assertNotIsInstance(adapter(connector).read_exact(within), ReadFailure)
        self.assertEqual(1, connector.read_calls)

        connector = SyntheticConnector()
        self.assertIsInstance(adapter(connector).read_exact(over), ReadFailure)
        self.assertEqual(0, connector.read_calls)
        self.assertEqual(0, connector.write_calls)

    def test_response_plain_tree_utf8_budget_has_exact_256_257_boundary(self):
        within = {"first": "a" * 128, "second": "界" * 39}
        over = {"first": "a" * 128, "second": "界" * 39 + "x"}
        self.assertEqual(256, plain_tree_utf8_bytes(within))
        self.assertEqual(257, plain_tree_utf8_bytes(over))

        connector = SyntheticConnector(read=within)
        subject = adapter(connector)
        self.assertNotIsInstance(subject.read_exact(action()), ReadFailure)
        self.assertEqual("success", subject.write_once(action()))
        self.assertEqual(1, connector.read_calls)
        self.assertEqual(1, connector.write_calls)

        connector = SyntheticConnector(read=over)
        subject = adapter(connector)
        self.assertIsInstance(subject.read_exact(action()), ReadFailure)
        self.assertNotEqual("success", subject.write_once(action()))
        self.assertEqual(1, connector.read_calls)
        self.assertEqual(0, connector.write_calls)

    def test_response_utf8_budget_stops_after_one_read_without_write_authority(self):
        connector = SyntheticConnector(read={"target": {"parent": "界" * 43}, "state": "absent"})
        subject = adapter(connector)
        result = subject.read_exact(action())
        self.assertIsInstance(result, ReadFailure)
        self.assertEqual(1, connector.read_calls)
        self.assertNotEqual("success", subject.write_once(action()))
        self.assertEqual(0, connector.write_calls)

    def test_mapping_keys_share_utf8_scalar_and_tree_budgets(self):
        within = {**action(), "payload": {"schema": {"a" * 128: "x"}}}
        connector = SyntheticConnector()
        self.assertNotIsInstance(adapter(connector).read_exact(within), ReadFailure)
        self.assertEqual(1, connector.read_calls)
        for key in ("a" * 129, "界" * 43):
            with self.subTest(kind="single-key", unicode=not key.isascii()):
                connector = SyntheticConnector()
                candidate = {**action(), "payload": {"schema": {key: "x"}}}
                self.assertIsInstance(adapter(connector).read_exact(candidate), ReadFailure)
                self.assertEqual(0, connector.read_calls)
        for schema in (
            {"a" * 90: "x", "b" * 90: "x", "c" * 90: "x"},
            {"界" * 30: "x", "域" * 30: "x", "值" * 30: "x"},
            {"k" * 128: "v" * 128, "z": "x" * 16},
        ):
            with self.subTest(kind="tree-key"):
                connector = SyntheticConnector()
                candidate = {**action(), "payload": {"schema": schema}}
                self.assertIsInstance(adapter(connector).read_exact(candidate), ReadFailure)
                self.assertEqual(0, connector.read_calls)

    def test_response_mapping_key_budget_stops_write_authority(self):
        responses = (
            {"target": {"parent": "synthetic-parent", "a" * 129: "x"}, "state": "absent"},
            {"target": {"parent": "synthetic-parent", "界" * 43: "x"}, "state": "absent"},
            {"target": {"parent": "synthetic-parent"}, "state": "absent", "items": {"k" * 90: "x" * 20, "q" * 90: "x" * 20, "r" * 90: "x" * 20}},
        )
        for response in responses:
            with self.subTest(key_kind="response"):
                connector = SyntheticConnector(read=response)
                subject = adapter(connector)
                result = subject.read_exact(action())
                self.assertIsInstance(result, ReadFailure)
                self.assertEqual(1, connector.read_calls)
                self.assertNotEqual("success", subject.write_once(action()))
                self.assertEqual(0, connector.write_calls)

    def test_existing_list_kind_remains_outside_canonical_adapter(self):
        connector = SyntheticConnector()
        result = adapter(connector).write_once(action("existing-list.archive"))
        self.assertNotEqual("success", result)
        self.assertEqual(0, connector.read_calls)
        self.assertEqual(0, connector.write_calls)


if __name__ == "__main__":
    unittest.main()
