"""T9 red gate: journal-bound execution through WriteCoordinator.run only."""

import copy
import contextlib
import dataclasses
import io
import inspect
import json
import logging
import threading
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

from new_system_approval import ApprovalEnvelope, ApprovalLedger, preview_next_action
from new_system_connector_action_adapter import CanonicalConnectorActionAdapter
from new_system_connector_entry_bridge import ConnectorEntryWireBridge
from new_system_recovery_journal import RecoveryJournal
from new_system_write_coordinator import WriteCoordinator


MISSING = object()
CANARY = "t9-private-content"
CANARY_ID = "t9-private-id"
CANARY_URL = "https://example.invalid/t9-private"
CANARY_TOKEN = "t9-private-token"


def action(kind="create_database"):
    return {"kind": kind, "target": {"parent": CANARY_ID}, "payload": {"schema": {"title": "synthetic"}}}


def archive_action(display_name="Synthetic Archive"):
    return {
        "kind": "create_archive_target",
        "target": {"container": CANARY_ID, "category": "slot-1"},
        "payload": {
            "display_name": display_name,
            "schema": {
                "Task": "title",
                "Takeaway": "rich_text",
                "Improvement": "rich_text",
            },
        },
    }


def state(value="absent"):
    return {"target": {"parent": CANARY_ID}, "state": value}


def facts(digest, fingerprint, phase):
    return {"action_digest": digest, "attempt_fingerprint": fingerprint,
            "read_attempted": phase != "prepared",
            "write_attempted": phase in {"write-started", "confirmed-applied", "confirmed-not-applied", "outcome-unknown", "possible-partial"},
            "write_started": phase in {"write-started", "confirmed-applied", "confirmed-not-applied", "outcome-unknown", "possible-partial"}}


class Connector:
    def __init__(self, read=None, write="success", events=None, entered=None, release=None):
        self.read = state() if read is None else read
        self.write = write
        self.events = [] if events is None else events
        self.entered, self.release = entered, release
        self.read_calls = self.write_calls = 0

    def read_canonical(self, _request):
        self.events.append("read"); self.read_calls += 1
        if self.entered: self.entered.set(); self.release.wait(1)
        if isinstance(self.read, BaseException): raise self.read
        return self.read

    def write_canonical(self, _request):
        self.events.append("write"); self.write_calls += 1
        if isinstance(self.write, BaseException): raise self.write
        return self.write


class T9RedGate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def system(self, *, root=None, read=None, write="success", events=None, entered=None, release=None):
        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(action(), state()), accepted=True)
        connector = Connector(read, write, events, entered, release)
        return ledger, envelope, CanonicalConnectorActionAdapter(connector), connector, RecoveryJournal(self.root if root is None else root)

    def execute(self, coordinator, envelope, adapter, journal, *, current=MISSING, expected=MISSING, **options):
        if "recovery_journal" not in inspect.signature(WriteCoordinator.run).parameters:
            self.fail("T9 requires WriteCoordinator.run(..., *, recovery_journal=...)")
        return coordinator.run(envelope, action() if current is MISSING else current,
                               state() if expected is MISSING else expected, adapter,
                               recovery_journal=journal, **options)

    def test_claim_phase_order_and_exact_facts_precede_io(self):
        events = []; ledger, envelope, adapter, connector, journal = self.system(events=events)
        original = RecoveryJournal.transition; claimed = []; captured = []; original_claim = ApprovalLedger.claim_for_write
        def claim(current, supplied):
            decision = original_claim(current, supplied)
            if current is ledger: claimed.append(decision)
            return decision
        def transition(instance, phase, supplied):
            if instance is journal: events.append("journal:" + phase); captured.append((phase, dict(supplied)))
            return original(instance, phase, supplied)
        with mock.patch.object(RecoveryJournal, "transition", new=transition), mock.patch.object(ApprovalLedger, "claim_for_write", new=claim):
            result = self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
        self.assertEqual(["prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"], [phase for phase, _ in captured])
        self.assertEqual(["journal:prepared", "read", "journal:read-completed", "journal:write-ready", "write", "journal:write-started", "journal:confirmed-applied"], events)
        self.assertEqual(1, len(claimed)); self.assertTrue(all(value["action_digest"] == claimed[0].action_digest and value["attempt_fingerprint"] == claimed[0].attempt_fingerprint for _, value in captured))
        expected_facts = {"prepared": (False, False, False), "read-completed": (True, False, False), "write-ready": (True, False, False), "write-started": (True, True, True), "confirmed-applied": (True, True, True)}
        self.assertEqual(expected_facts, {phase: (value["read_attempted"], value["write_attempted"], value["write_started"]) for phase, value in captured})
        self.assertTrue(result.authorized)

    def test_archive_title_binding_is_identical_across_preview_journal_and_adapter_request(self):
        class CapturingConnector(Connector):
            def read_canonical(self, request):
                self.read_request = request
                return super().read_canonical(request)

            def write_canonical(self, request):
                self.write_request = request
                return super().write_canonical(request)

        current = archive_action("研究 é")
        ledger = ApprovalLedger()
        preview = ledger.preview(current, state())
        envelope = ledger.issue(preview, accepted=True)
        connector = CapturingConnector()
        journal = RecoveryJournal(self.root)
        captured = []
        original = RecoveryJournal.transition

        def transition(instance, phase, supplied):
            if instance is journal:
                captured.append((phase, dict(supplied)))
            return original(instance, phase, supplied)

        with mock.patch.object(RecoveryJournal, "transition", new=transition):
            result = WriteCoordinator(ledger).run(
                envelope,
                current,
                state(),
                CanonicalConnectorActionAdapter(connector),
                recovery_journal=journal,
            )

        expected_request = {
            "parent": {"page_id": CANARY_ID},
            "title": "研究 é",
            "schema": 'CREATE TABLE ("Task" TITLE, "Takeaway" RICH_TEXT, "Improvement" RICH_TEXT)',
        }
        self.assertEqual("completed", result.status)
        self.assertEqual(expected_request, connector.read_request)
        self.assertEqual(expected_request, connector.write_request)
        self.assertTrue(captured)
        self.assertTrue(
            all(item["action_digest"] == preview.action_digest for _, item in captured)
        )

    def test_archive_title_change_after_approval_fails_before_journal_or_connector_io(self):
        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(archive_action("Archive A"), state()), accepted=True
        )
        connector = Connector()
        journal = RecoveryJournal(self.root)
        transitions = []
        original = RecoveryJournal.transition

        def transition(instance, phase, supplied):
            if instance is journal:
                transitions.append(phase)
            return original(instance, phase, supplied)

        with mock.patch.object(RecoveryJournal, "transition", new=transition):
            result = WriteCoordinator(ledger).run(
                envelope,
                archive_action("Archive B"),
                state(),
                CanonicalConnectorActionAdapter(connector),
                recovery_journal=journal,
            )

        self.assertFalse(result.authorized)
        self.assertEqual([], transitions)
        self.assertEqual((0, 0), (connector.read_calls, connector.write_calls))

    def test_archive_schema_change_after_approval_fails_before_journal_or_connector_io(self):
        current = archive_action()
        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(current, state()), accepted=True)
        current["payload"]["schema"] = {
            "Task": "title",
            "Takeaway": "rich_text",
        }
        connector = Connector()
        transitions = []
        journal = RecoveryJournal(self.root / "schema-drift")
        original = RecoveryJournal.transition

        def transition(instance, phase, supplied):
            if instance is journal:
                transitions.append(phase)
            return original(instance, phase, supplied)

        with mock.patch.object(RecoveryJournal, "transition", new=transition):
            result = WriteCoordinator(ledger).run(
                envelope,
                current,
                state(),
                CanonicalConnectorActionAdapter(connector),
                recovery_journal=journal,
            )

        self.assertFalse(result.authorized)
        self.assertEqual([], transitions)
        self.assertEqual((0, 0), (connector.read_calls, connector.write_calls))

    def test_archive_dynamic_schema_protocol_fails_before_journal_or_connector_io(self):
        calls = []

        class EvilText(str):
            __hash__ = str.__hash__

            def __eq__(self, _other):
                calls.append("eq")
                raise RuntimeError(CANARY)

        approved_action = archive_action()
        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(approved_action, state()), accepted=True
        )
        supplied = archive_action()
        supplied["payload"]["schema"]["Task"] = EvilText("title")
        connector = Connector()
        journal = RecoveryJournal(self.root / "dynamic-schema")
        transitions = []
        original = RecoveryJournal.transition

        def transition(instance, phase, facts):
            if instance is journal:
                transitions.append(phase)
            return original(instance, phase, facts)

        with mock.patch.object(RecoveryJournal, "transition", new=transition):
            result = WriteCoordinator(ledger).run(
                envelope,
                supplied,
                state(),
                CanonicalConnectorActionAdapter(connector),
                recovery_journal=journal,
            )

        self.assertFalse(result.authorized)
        self.assertEqual([], calls)
        self.assertEqual([], transitions)
        self.assertEqual((0, 0), (connector.read_calls, connector.write_calls))

    def assert_archive_drift_facts(self, mutation_phase, *, connector_class=Connector):
        current = archive_action("Archive A")
        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(current, state()), accepted=True)
        connector = connector_class()
        root = self.root / mutation_phase
        journal = RecoveryJournal(root)
        original = RecoveryJournal.transition

        def transition(instance, phase, supplied):
            result = original(instance, phase, supplied)
            if instance is journal and phase == mutation_phase:
                current["payload"]["display_name"] = "Archive B"
            return result

        with mock.patch.object(RecoveryJournal, "transition", new=transition):
            result = WriteCoordinator(ledger).run(
                envelope,
                current,
                state(),
                CanonicalConnectorActionAdapter(connector),
                recovery_journal=journal,
            )

        self.assertNotEqual("completed", result.status)
        self.assertFalse(result.authorized)
        self.assertNotIn("Archive A", json.dumps(result.to_public_dict(), sort_keys=True))
        self.assertNotIn("Archive B", json.dumps(result.to_public_dict(), sort_keys=True))
        self.assertEqual(0, connector.write_calls)
        leaf = root / ".todo_archive" / "new_system_recovery.json"
        durable = json.loads(leaf.read_text())
        self.assertEqual("interrupted", durable["phase"])
        self.assertEqual(connector.read_calls == 1, durable["read_attempted"])
        self.assertFalse(durable["write_attempted"])
        self.assertFalse(durable["write_started"])
        resumed = RecoveryJournal(root).resume_review().to_public_dict()
        self.assertEqual("fresh-review-required", resumed["status"])
        self.assertEqual("fresh-reinspection", resumed["classification"])
        self.assertFalse(resumed["write_authority"])
        return connector

    def test_post_prepared_action_drift_has_zero_io_and_truthful_restart_facts(self):
        connector = self.assert_archive_drift_facts("prepared")
        self.assertEqual((0, 0), (connector.read_calls, connector.write_calls))

    def test_post_read_completed_action_drift_has_read_only_facts(self):
        connector = self.assert_archive_drift_facts("read-completed")
        self.assertEqual((1, 0), (connector.read_calls, connector.write_calls))

    def test_post_durable_pre_io_gate_drift_never_claims_write_attempted(self):
        connector = self.assert_archive_drift_facts("write-ready")
        self.assertEqual((1, 0), (connector.read_calls, connector.write_calls))

    def test_adapter_pre_io_revocation_after_write_ready_records_no_write_attempt(self):
        ledger, envelope, adapter, connector, journal = self.system(
            root=self.root / "adapter-pre-io-revoked"
        )
        original = RecoveryJournal.transition

        def transition(instance, phase, supplied):
            result = original(instance, phase, supplied)
            if instance is journal and phase == "write-ready":
                adapter.read_exact({})
            return result

        with mock.patch.object(RecoveryJournal, "transition", new=transition):
            result = self.execute(
                WriteCoordinator(ledger), envelope, adapter, journal
            )

        self.assertFalse(result.authorized)
        self.assertEqual((1, 0), (connector.read_calls, connector.write_calls))
        leaf = self.root / "adapter-pre-io-revoked" / ".todo_archive" / "new_system_recovery.json"
        durable = json.loads(leaf.read_text())
        self.assertEqual(
            ("not-invoked", True, False, False),
            tuple(
                durable[key]
                for key in (
                    "phase",
                    "read_attempted",
                    "write_attempted",
                    "write_started",
                )
            ),
        )
        resumed = RecoveryJournal(self.root / "adapter-pre-io-revoked").resume_review().to_public_dict()
        self.assertEqual("not-invoked", resumed["classification"])
        self.assertFalse(resumed["write_authority"])

    def test_trusted_adapter_attempt_evidence_drives_durable_write_facts(self):
        for index, (outcome, terminal) in enumerate((
            ("success", "confirmed-applied"),
            (RuntimeError(CANARY), "outcome-unknown"),
            ("ambiguous", "outcome-unknown"),
            ("partial", "possible-partial"),
        )):
            with self.subTest(outcome=type(outcome).__name__ + str(outcome)):
                root = self.root / ("attempt-evidence-" + str(index))
                ledger, envelope, adapter, connector, journal = self.system(root=root, write=outcome)
                self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
                self.assertEqual((1, 1), (connector.read_calls, connector.write_calls))
                durable = json.loads((root / ".todo_archive" / "new_system_recovery.json").read_text())
                self.assertEqual(terminal, durable["phase"])
                self.assertTrue(durable["write_attempted"])
                self.assertTrue(durable["write_started"])

    def test_missing_connector_write_entry_is_not_invoked_and_never_uncertain(self):
        class ReadOnlyConnector:
            def __init__(self): self.read_calls = 0
            def read_canonical(self, _request):
                self.read_calls += 1
                return state()

        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(action(), state()), accepted=True)
        connector = ReadOnlyConnector()
        root = self.root / "not-invoked"
        result = WriteCoordinator(ledger).run(
            envelope,
            action(),
            state(),
            CanonicalConnectorActionAdapter(connector),
            recovery_journal=RecoveryJournal(root),
        )
        self.assertEqual("not-invoked", result.reason)
        self.assertTrue(result.approval_consumed)
        self.assertFalse(result.write_attempted)
        self.assertEqual("not-attempted", result.outcome_certainty)
        durable = json.loads(
            (root / ".todo_archive" / "new_system_recovery.json").read_text()
        )
        self.assertEqual(
            ("not-invoked", False, False),
            (durable["phase"], durable["write_attempted"], durable["write_started"]),
        )
        self.assertNotIn(durable["phase"], {"outcome-unknown", "possible-partial"})
        resumed = RecoveryJournal(root).resume_review().to_public_dict()
        self.assertEqual("not-invoked", resumed["classification"])
        self.assertFalse(resumed["write_authority"])

    def test_signature_failure_is_not_invoked_but_body_exception_is_attempted(self):
        class BadSignature(Connector):
            def write_canonical(self):
                raise AssertionError("must not enter")
        class BodyTypeError(Connector):
            def write_canonical(self, _request):
                self.write_calls += 1
                raise TypeError("inside body")
        for index, (connector, expected_phase, attempted) in enumerate((
            (BadSignature(), "not-invoked", False),
            (BodyTypeError(), "outcome-unknown", True),
        )):
            with self.subTest(phase=expected_phase):
                ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action(), state()), accepted=True)
                root = self.root / ("signature-" + str(index))
                result = WriteCoordinator(ledger).run(
                    envelope, action(), state(), CanonicalConnectorActionAdapter(connector),
                    recovery_journal=RecoveryJournal(root),
                )
                durable = json.loads((root / ".todo_archive" / "new_system_recovery.json").read_text())
                self.assertEqual(expected_phase, durable["phase"])
                self.assertEqual(attempted, durable["write_attempted"])
                self.assertEqual(int(attempted), connector.write_calls)
                self.assertNotEqual("completed", result.status)

    def test_wire_bridge_entry_evidence_drives_not_invoked_unknown_and_attempted_facts(self):
        ledger = ApprovalLedger(); preview = ledger.preview(action(), state())
        envelope = ledger.issue(preview, accepted=True)
        transcript = io.BytesIO(b'{"type":"connector-outcome","outcome":"success"}\n')
        root = self.root / "wire-unsupported"
        bridge = ConnectorEntryWireBridge(transcript, io.BytesIO(), state())
        result = WriteCoordinator(ledger).run(
            envelope, action(), state(), CanonicalConnectorActionAdapter(bridge),
            recovery_journal=RecoveryJournal(root),
        )
        durable = json.loads((root / ".todo_archive" / "new_system_recovery.json").read_text())
        self.assertEqual("entry-unknown", durable["phase"])
        self.assertFalse(durable["write_attempted"])
        self.assertEqual("unknown", result.outcome_certainty)
        self.assertNotEqual("completed", result.status)

    def test_dynamic_or_noncanonical_adapter_cannot_forge_attempt_evidence(self):
        class DynamicOutcome:
            def __getattr__(self, _name):
                raise RuntimeError(CANARY)

        class MaliciousAdapter:
            def __init__(self):
                self.read_calls = self.write_calls = 0
            def read_exact(self, _action):
                self.read_calls += 1
                return state()
            def write_once(self, _action):
                self.write_calls += 1
                return DynamicOutcome()

        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(action(), state()), accepted=True)
        adapter = MaliciousAdapter()
        journal = RecoveryJournal(self.root / "malicious-adapter")
        result = WriteCoordinator(ledger).run(
            envelope, action(), state(), adapter, recovery_journal=journal
        )
        self.assertFalse(result.authorized)
        self.assertEqual((0, 0), (adapter.read_calls, adapter.write_calls))
        self.assertFalse(result.write_attempted)
        public = json.dumps(result.to_public_dict(), sort_keys=True)
        self.assertNotIn(CANARY, public)

    def test_restore_then_change_combination_uses_snapshot_and_still_stops_write(self):
        current_holder = {}

        class RestoreConnector(Connector):
            def read_canonical(self, request):
                current = current_holder["action"]
                current["payload"]["display_name"] = "Archive B"
                current["payload"]["display_name"] = "Archive A"
                return super().read_canonical(request)

        current = archive_action("Archive A")
        current_holder["action"] = current
        ledger = ApprovalLedger()
        envelope = ledger.issue(ledger.preview(current, state()), accepted=True)
        connector = RestoreConnector()
        root = self.root / "restore-then-change"
        journal = RecoveryJournal(root)
        original = RecoveryJournal.transition
        def transition(instance, phase, supplied):
            result = original(instance, phase, supplied)
            if instance is journal and phase == "read-completed":
                current["payload"]["display_name"] = "Archive B"
            return result
        with mock.patch.object(RecoveryJournal, "transition", new=transition):
            result = WriteCoordinator(ledger).run(envelope, current, state(), CanonicalConnectorActionAdapter(connector), recovery_journal=journal)
        self.assertFalse(result.authorized)
        self.assertEqual((1, 0), (connector.read_calls, connector.write_calls))
        durable = json.loads((root / ".todo_archive" / "new_system_recovery.json").read_text())
        self.assertEqual(("interrupted", True, False, False), tuple(durable[key] for key in ("phase", "read_attempted", "write_attempted", "write_started")))
        resumed = RecoveryJournal(root).resume_review().to_public_dict()
        self.assertEqual("fresh-reinspection", resumed["classification"])
        self.assertNotIn("Archive", json.dumps(result.to_public_dict(), sort_keys=True))

    def test_each_journal_phase_failure_stops_at_the_factual_io_boundary(self):
        limits = {"prepared": (0, 0), "read-completed": (1, 0), "write-ready": (1, 0), "write-started": (1, 1)}
        for failed, expected in limits.items():
            with self.subTest(failed=failed):
                root = self.root / ("exception-" + failed); ledger, envelope, adapter, connector, journal = self.system(root=root); original = RecoveryJournal.transition
                def transition(instance, phase, supplied):
                    if instance is journal and phase == failed: raise OSError(CANARY)
                    return original(instance, phase, supplied)
                with mock.patch.object(RecoveryJournal, "transition", new=transition):
                    self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
                self.assertEqual(expected, (connector.read_calls, connector.write_calls))
                retry = self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
                self.assertEqual(expected, (connector.read_calls, connector.write_calls))
                self.assertFalse(retry.to_public_dict().get("write_authority", False))

    def test_each_terminal_rejection_or_exception_keeps_write_started_and_restart_gate(self):
        cases = (("success", "confirmed-applied"), ("changed", "confirmed-not-applied"), ("partial", "possible-partial"), ("ambiguous", "outcome-unknown"))
        for outcome, terminal in cases:
            for rejected in (False, True):
                with self.subTest(terminal=terminal, rejected=rejected):
                    root = self.root / (terminal + str(rejected)); ledger, envelope, adapter, connector, journal = self.system(root=root, write=outcome)
                    original = RecoveryJournal.transition
                    class Rejected:
                        def to_public_dict(self): return {"status": "rejected", "phase": "write-started", "classification": "fresh-reinspection", "failure_class": "storage-failed", "write_authority": False, "review_generated": False}
                    def transition(instance, phase, supplied):
                        if instance is journal and phase == terminal:
                            if rejected: return Rejected()
                            raise OSError(CANARY)
                        return original(instance, phase, supplied)
                    with mock.patch.object(RecoveryJournal, "transition", new=transition):
                        result = self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
                    self.assertNotEqual("completed", result.to_public_dict()["status"]); self.assertFalse(result.to_public_dict().get("write_authority", False))
                    self.assertEqual((1, 1), (connector.read_calls, connector.write_calls))
                    self.assertEqual("write-started", journal.resume_review().to_public_dict()["phase"])
                    fresh = RecoveryJournal(root)
                    self.assertEqual("write-started", fresh.resume_review().to_public_dict()["phase"])
                    retry = self.execute(WriteCoordinator(ledger), envelope, adapter, fresh)
                    self.assertEqual((1, 1), (connector.read_calls, connector.write_calls)); self.assertFalse(retry.to_public_dict().get("write_authority", False))

    def test_rejected_prewrite_transition_result_blocks_the_matching_boundary(self):
        limits = {"prepared": (0, 0), "read-completed": (1, 0), "write-ready": (1, 0), "write-started": (1, 1)}
        class Rejected:
            def to_public_dict(self): return {"status": "rejected", "phase": None, "classification": "fresh-reinspection", "failure_class": "storage-failed", "write_authority": False, "review_generated": False}
        for failed, expected in limits.items():
            with self.subTest(failed=failed):
                root = self.root / ("rejected-" + failed); ledger, envelope, adapter, connector, journal = self.system(root=root); original = RecoveryJournal.transition
                def transition(instance, phase, supplied):
                    return Rejected() if instance is journal and phase == failed else original(instance, phase, supplied)
                with mock.patch.object(RecoveryJournal, "transition", new=transition):
                    result = self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
                self.assertEqual(expected, (connector.read_calls, connector.write_calls)); self.assertNotEqual("completed", result.to_public_dict()["status"])

    def test_closed_outcome_map_is_durable_and_single_write(self):
        cases = (("success", "confirmed-applied"), ("changed", "confirmed-not-applied"), ("no-op", "confirmed-not-applied"), ("partial", "possible-partial"), ("ambiguous", "outcome-unknown"), (object(), "outcome-unknown"), (RuntimeError(CANARY), "outcome-unknown"))
        for index, (outcome, phase) in enumerate(cases):
            with self.subTest(index=index, outcome=type(outcome).__name__):
                root = self.root / ("outcome-" + str(index) + "-" + phase); ledger, envelope, adapter, connector, journal = self.system(root=root, write=outcome)
                self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
                self.assertEqual(phase, journal.resume_review().to_public_dict()["phase"])
                self.assertEqual((1, 1), (connector.read_calls, connector.write_calls))

    def test_restart_pending_and_inflight_journals_are_review_only(self):
        for phase in ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"):
            with self.subTest(phase=phase):
                root = self.root / ("restart-" + phase); ledger, envelope, adapter, connector, initial = self.system(root=root)
                initial.transition("prepared", facts("a" * 64, "b" * 32, "prepared"))
                if phase != "prepared": initial.transition("read-completed", facts("a" * 64, "b" * 32, "read-completed"))
                if phase not in {"prepared", "read-completed"}: initial.transition("write-ready", facts("a" * 64, "b" * 32, "write-ready"))
                if phase in {"write-started", "confirmed-applied"}: initial.transition("write-started", facts("a" * 64, "b" * 32, "write-started"))
                if phase == "confirmed-applied": initial.transition("confirmed-applied", facts("a" * 64, "b" * 32, "confirmed-applied"))
                result = self.execute(WriteCoordinator(ledger), envelope, adapter, RecoveryJournal(root))
                self.assertEqual((0, 0), (connector.read_calls, connector.write_calls))
                self.assertFalse(result.authorized)
        dirty_root = self.root / "restart-foreign"; dirty_root.joinpath(".todo_archive").mkdir(parents=True, mode=0o700)
        dirty = dirty_root / ".todo_archive" / ".new-system-inflight-foreign"; dirty.write_bytes(b""); dirty.chmod(0o600)
        ledger, envelope, adapter, connector, journal = self.system(root=dirty_root)
        self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
        self.assertEqual((0, 0), (connector.read_calls, connector.write_calls))

    def test_missing_forged_consumed_different_and_drift_are_zero_write(self):
        for kind in ("missing", "forged", "consumed", "different", "expected-drift", "observed-drift"):
            with self.subTest(kind=kind):
                root = self.root / ("drift-" + kind); ledger, envelope, adapter, connector, journal = self.system(root=root)
                supplied, expected = action(), state()
                if kind == "missing": envelope = None
                elif kind == "forged": envelope = ApprovalEnvelope()
                elif kind == "consumed": ledger.consume(envelope, action(), state())
                elif kind == "different": supplied = {"kind": "create_archive_container", "target": {"parent": CANARY_ID}, "payload": {"title": "other"}}
                elif kind == "expected-drift": expected = state("changed")
                elif kind == "observed-drift": connector.read = state("changed")
                self.execute(WriteCoordinator(ledger), envelope, adapter, journal, current=supplied, expected=expected)
                self.assertEqual(0, connector.write_calls)

    def test_threaded_duplicate_and_callback_reentry_have_at_most_one_path(self):
        if "recovery_journal" not in inspect.signature(WriteCoordinator.run).parameters:
            self.fail("T9 requires WriteCoordinator.run(..., *, recovery_journal=...)")
        entered, release = threading.Event(), threading.Event(); root = self.root / "concurrency"
        ledger, envelope, adapter, connector, journal = self.system(root=root, entered=entered, release=release); original = RecoveryJournal.transition; phases = []
        def transition(instance, phase, supplied):
            if instance is journal: phases.append((phase, dict(supplied)))
            return original(instance, phase, supplied)
        first, second = WriteCoordinator(ledger), WriteCoordinator(ledger); results = []
        with mock.patch.object(RecoveryJournal, "transition", new=transition):
            thread = threading.Thread(target=lambda: results.append(self.execute(first, envelope, adapter, journal)))
            thread.start(); self.assertTrue(entered.wait(1))
            results.append(self.execute(second, envelope, adapter, journal)); release.set(); thread.join(1)
        self.assertFalse(thread.is_alive()); self.assertLessEqual(connector.read_calls, 1); self.assertLessEqual(connector.write_calls, 1)
        self.assertTrue(phases); self.assertEqual(len({phase for phase, _ in phases}), len(phases)); self.assertEqual("prepared", phases[0][0]); self.assertIsNotNone(RecoveryJournal(root).resume_review().to_public_dict()["phase"])
        with self.assertRaises(TypeError): copy.copy(adapter)
        ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action(), state()), accepted=True)
        nested = []; coordinator = WriteCoordinator(ledger); journal = RecoveryJournal(self.root / "callback")
        class ReentrantConnector(Connector):
            def read_canonical(current, request):
                nested.append(self.execute(coordinator, envelope, adapter, journal))
                return super().read_canonical(request)
        connector = ReentrantConnector(); adapter = CanonicalConnectorActionAdapter(connector)
        self.execute(coordinator, envelope, adapter, journal)
        self.assertEqual(1, len(nested)); self.assertLessEqual(connector.read_calls, 1); self.assertLessEqual(connector.write_calls, 1)

    def test_interrupt_dynamic_and_malicious_journal_values_are_sanitized(self):
        for moment, expected_calls, expected_facts in (("before", (0, 0), (False, False, False)), ("after", (1, 0), (True, False, False))):
            with self.subTest(moment=moment):
                root = self.root / ("interrupt-" + moment); ledger, envelope, adapter, connector, journal = self.system(root=root); original = RecoveryJournal.transition; captured = []
                def transition(instance, phase, supplied):
                    if instance is journal: captured.append((phase, dict(supplied)))
                    return original(instance, phase, supplied)
                with mock.patch.object(RecoveryJournal, "transition", new=transition):
                    result = self.execute(WriteCoordinator(ledger), envelope, adapter, journal, **{"interrupt_before_read" if moment == "before" else "interrupt_after_read": True})
                self.assertEqual(expected_calls, (connector.read_calls, connector.write_calls)); self.assertTrue(captured); self.assertEqual("interrupted", captured[-1][0])
                self.assertEqual(expected_facts, tuple(captured[-1][1][key] for key in ("read_attempted", "write_attempted", "write_started")))
                self.assertFalse(result.to_public_dict().get("write_authority", False))
        class EvilResult:
            def to_public_dict(self): raise RuntimeError(CANARY_TOKEN)
            def __repr__(self): raise RuntimeError(CANARY_URL)
        original = RecoveryJournal.transition
        def transition(instance, phase, supplied):
            if instance is journal: return EvilResult()
            return original(instance, phase, supplied)
        ledger, envelope, adapter, connector, journal = self.system(root=self.root / "privacy")
        stdout, stderr, logged = io.StringIO(), io.StringIO(), io.StringIO(); logger = logging.getLogger("t9-red-gate"); handler = logging.StreamHandler(logged); logger.addHandler(handler); caught = ""
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), mock.patch.object(RecoveryJournal, "transition", new=transition):
                try: result = self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
                except Exception: caught = traceback.format_exc(); result = None
        finally: logger.removeHandler(handler)
        public = ("" if result is None else repr(result) + json.dumps(result.to_public_dict(), sort_keys=True)) + stdout.getvalue() + stderr.getvalue() + logged.getvalue() + caught
        for secret in (CANARY, CANARY_ID, CANARY_URL, CANARY_TOKEN, str(self.root)): self.assertNotIn(secret, public)
        self.assertEqual((0, 0), (connector.read_calls, connector.write_calls))

    def test_journal_bound_public_result_is_closed(self):
        ledger, envelope, adapter, connector, journal = self.system(write=RuntimeError(CANARY))
        result = self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
        public = result.to_public_dict()
        self.assertIsNone(public["action_digest"]); self.assertIsNone(public["attempt_fingerprint"])
        self.assertNotIn(CANARY, json.dumps(public, sort_keys=True) + repr(result))

    def test_journal_bound_dataclass_surfaces_never_expose_claim_binding(self):
        for outcome in ("success", "changed", "partial", "ambiguous"):
            with self.subTest(outcome=outcome):
                root = self.root / ("dataclass-" + outcome); ledger, envelope, adapter, connector, journal = self.system(root=root, write=outcome)
                result = self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
                public = repr(result) + repr(result.recovery_preview) + json.dumps(dataclasses.asdict(result), sort_keys=True) + json.dumps(result.to_public_dict(), sort_keys=True)
                self.assertIsNone(result.action_digest); self.assertIsNone(result.attempt_fingerprint)
                self.assertIsNone(result.recovery_preview.action_digest if result.recovery_preview else None)
                self.assertNotIn(preview_next_action(action(), state()).action_digest, public)

    def test_old_dataclass_field_signature_repr_and_asdict_surface_is_unchanged(self):
        from new_system_write_coordinator import RecoveryPreview, WriteResult
        self.assertEqual(["status", "reason", "action_digest", "attempt_fingerprint", "phase", "reinspection_required", "new_preview_required"], [field.name for field in dataclasses.fields(RecoveryPreview)])
        self.assertEqual(["status", "reason", "authorized", "approval_consumed", "read_attempted", "write_attempted", "outcome_certainty", "reinspection_required", "new_preview_required", "action_digest", "attempt_fingerprint", "phase", "recovery_preview"], [field.name for field in dataclasses.fields(WriteResult)])
        self.assertEqual([field.name for field in dataclasses.fields(RecoveryPreview)], list(inspect.signature(RecoveryPreview).parameters))
        self.assertEqual([field.name for field in dataclasses.fields(WriteResult)], list(inspect.signature(WriteResult).parameters))
        ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action(), state()), accepted=True)
        completed = WriteCoordinator(ledger).run(envelope, action(), state(), CanonicalConnectorActionAdapter(Connector()))
        self.assertEqual([field.name for field in dataclasses.fields(WriteResult)], list(dataclasses.asdict(completed)))
        self.assertEqual(
            "WriteResult(status='completed', reason='completed', authorized=True, approval_consumed=True, read_attempted=True, write_attempted=True, outcome_certainty='confirmed', reinspection_required=False, new_preview_required=False, action_digest=None, attempt_fingerprint=None, phase='completed', recovery_preview=None)",
            repr(WriteResult("completed", "completed", True, True, True, True, "confirmed", False, False, None, None, "completed", None)),
        )
        ledger = ApprovalLedger(); envelope = ledger.issue(ledger.preview(action(), state()), accepted=True)
        recovery = WriteCoordinator(ledger).run(envelope, action(), state(), CanonicalConnectorActionAdapter(Connector(write="partial")))
        self.assertEqual([field.name for field in dataclasses.fields(WriteResult)], list(dataclasses.asdict(recovery)))
        self.assertEqual([field.name for field in dataclasses.fields(RecoveryPreview)], list(dataclasses.asdict(recovery.recovery_preview)))
        self.assertEqual(
            "RecoveryPreview(status='recovery-review-required', reason='partial', action_digest=None, attempt_fingerprint=None, phase='write', reinspection_required=True, new_preview_required=True)",
            repr(RecoveryPreview("recovery-review-required", "partial", None, None, "write", True, True)),
        )

    def test_read_attempt_failure_persists_true_read_fact_and_no_write_fact(self):
        for index, read in enumerate((RuntimeError(CANARY), {}, {"kind": "no-progress"})):
            with self.subTest(index=index):
                root = self.root / ("read-failure-" + str(index)); ledger, envelope, adapter, connector, journal = self.system(root=root, read=read); original = RecoveryJournal.transition; captured = []
                def transition(instance, phase, supplied):
                    if instance is journal: captured.append((phase, dict(supplied)))
                    return original(instance, phase, supplied)
                with mock.patch.object(RecoveryJournal, "transition", new=transition):
                    result = self.execute(WriteCoordinator(ledger), envelope, adapter, journal)
                interrupted = [value for phase, value in captured if phase == "interrupted"]
                self.assertTrue(interrupted); self.assertEqual((True, False, False), tuple(interrupted[-1][key] for key in ("read_attempted", "write_attempted", "write_started")))
                self.assertEqual((1, 0), (connector.read_calls, connector.write_calls)); self.assertFalse(result.authorized)


if __name__ == "__main__": unittest.main()
