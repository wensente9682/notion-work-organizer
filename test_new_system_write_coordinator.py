import dataclasses
import json
import threading
import unittest
from collections.abc import Mapping

from new_system_approval import ApprovalEnvelope, ApprovalLedger
from new_system_write_coordinator import ReadFailure, WriteCoordinator


CANARY_ID = "db-secret-123"
CANARY_URL = "https://notion.so/secret-page"
CANARY_TASK = "private task text"
CANARY_CATEGORY = "Private Category"
CANARY_STATE = "private expected state"


def action():
    return {
        "kind": "create_database",
        "target": {"parent_id": CANARY_ID, "url": CANARY_URL},
        "payload": {"title": CANARY_TASK, "category": CANARY_CATEGORY},
    }


def state(value=CANARY_STATE):
    return {"target": {"id": CANARY_ID}, "state": value}


def approved(ledger):
    return ledger.issue(ledger.preview(action(), state()), accepted=True)


class FakeAdapter:
    def __init__(self, read_value=None, write_value="success"):
        self.read_value = state() if read_value is None else read_value
        self.write_value = write_value
        self.read_count = 0
        self.write_count = 0

    def read_exact(self, _action):
        self.read_count += 1
        if isinstance(self.read_value, BaseException):
            raise self.read_value
        return self.read_value

    def write_once(self, _action):
        self.write_count += 1
        if isinstance(self.write_value, BaseException):
            raise self.write_value
        return self.write_value


class NewSystemWriteCoordinatorTests(unittest.TestCase):
    def test_evil_envelope_is_rejected_without_adapter_io_or_leak(self):
        calls = []

        class EvilEnvelope(ApprovalEnvelope):
            def __hash__(self):
                calls.append("hash")
                raise RuntimeError(CANARY_TASK)

            def __eq__(self, _other):
                calls.append("eq")
                raise RuntimeError(CANARY_TASK)

            def __repr__(self):
                calls.append("repr")
                raise RuntimeError(CANARY_TASK)

        adapter = FakeAdapter()
        result = WriteCoordinator(ApprovalLedger()).run(
            EvilEnvelope(), action(), state(), adapter
        )
        public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True)

        self.assertEqual(result.reason, "approval-required")
        self.assertEqual(adapter.read_count, 0)
        self.assertEqual(adapter.write_count, 0)
        self.assertEqual(calls, [])
        self.assertNotIn(CANARY_TASK, public)
    def test_dynamic_canonicalization_failure_keeps_read_facts_and_is_sanitized(self):
        class ExplodingMapping(Mapping):
            def __getitem__(self, _key):
                raise RuntimeError(CANARY_TASK)

            def __iter__(self):
                raise RuntimeError(CANARY_TASK)

            def __len__(self):
                return 1

        ledger = ApprovalLedger()
        adapter = FakeAdapter(read_value=ExplodingMapping())
        envelope = approved(ledger)
        result = WriteCoordinator(ledger).run(envelope, action(), state(), adapter)
        retry = WriteCoordinator(ledger).run(envelope, action(), state(), adapter)
        public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True)

        self.assertTrue(result.read_attempted)
        self.assertFalse(result.write_attempted)
        self.assertEqual(result.phase, "read")
        self.assertEqual(adapter.read_count, 1)
        self.assertEqual(adapter.write_count, 0)
        self.assertEqual(retry.reason, "approval-terminal")
        self.assertNotIn(CANARY_TASK, public)

    def test_recovery_phase_matrix(self):
        cases = (
            (FakeAdapter(read_value=ReadFailure("ambiguous")), {}, "read"),
            (FakeAdapter(read_value=state("drift")), {}, "authorize"),
            (FakeAdapter(write_value="partial"), {}, "write"),
            (FakeAdapter(write_value=RuntimeError(CANARY_TASK)), {}, "write"),
            (FakeAdapter(), {"interrupt_before_read": True}, "claim"),
        )
        for adapter, options, phase in cases:
            with self.subTest(phase=phase):
                ledger = ApprovalLedger()
                result = WriteCoordinator(ledger).run(
                    approved(ledger), action(), state(), adapter, **options
                )
                self.assertEqual(result.phase, phase)
        ledger = ApprovalLedger()
        completed = WriteCoordinator(ledger).run(
            approved(ledger), action(), state(), FakeAdapter()
        )
        self.assertEqual(completed.phase, "completed")
    def test_second_coordinator_cannot_read_after_completion(self):
        ledger = ApprovalLedger()
        adapter = FakeAdapter()
        envelope = approved(ledger)

        first = WriteCoordinator(ledger).run(envelope, action(), state(), adapter)
        second = WriteCoordinator(ledger).run(envelope, action(), state(), adapter)

        self.assertEqual(first.status, "completed")
        self.assertEqual(second.reason, "approval-terminal")
        self.assertEqual(adapter.read_count, 1)
        self.assertEqual(adapter.write_count, 1)

    def test_cross_coordinator_reentry_returns_in_progress_without_io(self):
        ledger = ApprovalLedger()
        envelope = approved(ledger)
        first = WriteCoordinator(ledger)
        second = WriteCoordinator(ledger)
        nested = []

        class ReentrantAdapter(FakeAdapter):
            def read_exact(self, current_action):
                self.read_count += 1
                nested.append(second.run(envelope, current_action, state(), self))
                return state()

        adapter = ReentrantAdapter()
        result = first.run(envelope, action(), state(), adapter)

        self.assertEqual(result.status, "completed")
        self.assertEqual(nested[0].status, "attempt-in-progress")
        self.assertFalse(nested[0].approval_consumed)
        self.assertEqual(adapter.read_count, 1)
        self.assertEqual(adapter.write_count, 1)

    def test_same_coordinator_reentry_returns_in_progress_without_io(self):
        ledger = ApprovalLedger()
        envelope = approved(ledger)
        coordinator = WriteCoordinator(ledger)
        nested = []

        class ReentrantAdapter(FakeAdapter):
            def read_exact(self, current_action):
                self.read_count += 1
                nested.append(coordinator.run(envelope, current_action, state(), self))
                return state()

        adapter = ReentrantAdapter()
        result = coordinator.run(envelope, action(), state(), adapter)

        self.assertEqual(result.status, "completed")
        self.assertEqual(nested[0].status, "attempt-in-progress")
        self.assertEqual(adapter.read_count, 1)
        self.assertEqual(adapter.write_count, 1)

    def test_cross_coordinator_inflight_claim_has_one_read_write_path(self):
        ledger = ApprovalLedger()
        envelope = approved(ledger)
        entered = threading.Event()
        release = threading.Event()

        class BlockingAdapter(FakeAdapter):
            def read_exact(self, _action):
                self.read_count += 1
                entered.set()
                release.wait(1)
                return state()

        adapter = BlockingAdapter()
        first_result = []

        def first_run():
            first_result.append(
                WriteCoordinator(ledger).run(envelope, action(), state(), adapter)
            )

        thread = threading.Thread(target=first_run)
        thread.start()
        self.assertTrue(entered.wait(1))
        second = WriteCoordinator(ledger).run(envelope, action(), state(), adapter)
        release.set()
        thread.join()

        self.assertEqual(first_result[0].status, "completed")
        self.assertEqual(second.status, "attempt-in-progress")
        self.assertEqual(adapter.read_count, 1)
        self.assertEqual(adapter.write_count, 1)

    def test_failure_then_other_coordinator_has_no_second_read_or_write(self):
        ledger = ApprovalLedger()
        envelope = approved(ledger)
        adapter = FakeAdapter(read_value=ReadFailure("ambiguous"))

        first = WriteCoordinator(ledger).run(envelope, action(), state(), adapter)
        second = WriteCoordinator(ledger).run(envelope, action(), state(), adapter)

        self.assertEqual(first.status, "recovery-required")
        self.assertEqual(second.reason, "approval-terminal")
        self.assertEqual(adapter.read_count, 1)
        self.assertEqual(adapter.write_count, 0)

    def test_non_native_write_outcomes_cannot_compare_or_reenter(self):
        ledger = ApprovalLedger()
        coordinator = WriteCoordinator(ledger)
        envelope = approved(ledger)
        compared = []

        class FakeSuccess(str):
            def __eq__(self, _other):
                compared.append(True)
                return True

        adapter = FakeAdapter(write_value=FakeSuccess("success"))
        result = coordinator.run(envelope, action(), state(), adapter)

        self.assertEqual(result.reason, "outcome-unknown")
        self.assertFalse(compared)
        self.assertEqual(adapter.write_count, 1)

    def test_raising_outcome_cannot_leak_or_trigger_second_write(self):
        ledger = ApprovalLedger()
        coordinator = WriteCoordinator(ledger)
        envelope = approved(ledger)
        adapter = FakeAdapter()

        class RaisingOutcome:
            def __eq__(self, _other):
                adapter.write_once(action())
                raise RuntimeError(CANARY_TASK)

            def __repr__(self):
                raise RuntimeError(CANARY_URL)

        adapter.write_value = RaisingOutcome()
        result = coordinator.run(envelope, action(), state(), adapter)

        self.assertEqual(result.reason, "outcome-unknown")
        self.assertEqual(adapter.write_count, 1)
        self.assertNotIn(CANARY_TASK, repr(result))
        self.assertNotIn(CANARY_URL, repr(result))

    def test_invalid_read_failure_reason_is_static(self):
        for reason in ([], object()):
            with self.subTest(reason_type=type(reason).__name__):
                ledger = ApprovalLedger()
                result = WriteCoordinator(ledger).run(
                    approved(ledger),
                    action(),
                    state(),
                    FakeAdapter(read_value=ReadFailure(reason)),
                )
                self.assertEqual(result.reason, "read-unusable")
                self.assertTrue(result.approval_consumed)

    def test_claimed_result_has_safe_correlatable_attempt_fields(self):
        ledger = ApprovalLedger()
        adapter = FakeAdapter(read_value=ReadFailure("ambiguous"))
        result = WriteCoordinator(ledger).run(
            approved(ledger), action(), state(), adapter
        )
        public = json.dumps(result.to_public_dict(), sort_keys=True)

        self.assertEqual(len(result.action_digest), 64)
        self.assertGreaterEqual(len(result.attempt_fingerprint), 32)
        self.assertEqual(result.phase, "read")
        self.assertIn(result.action_digest, public)
        self.assertIn(result.attempt_fingerprint, public)
        self.assertNotIn(CANARY_ID, public)

    def test_unchanged_state_writes_once_then_same_approval_is_terminal(self):
        ledger = ApprovalLedger()
        adapter = FakeAdapter()
        coordinator = WriteCoordinator(ledger)
        envelope = approved(ledger)

        result = coordinator.run(envelope, action(), state(), adapter)
        retry = coordinator.run(envelope, action(), state(), adapter)

        self.assertEqual(result.status, "completed")
        self.assertEqual(adapter.read_count, 1)
        self.assertEqual(adapter.write_count, 1)
        self.assertTrue(result.approval_consumed)
        self.assertEqual(retry.reason, "approval-terminal")
        self.assertEqual(adapter.read_count, 1)
        self.assertEqual(adapter.write_count, 1)

    def test_missing_forged_and_cross_ledger_approval_never_write(self):
        ledger = ApprovalLedger()
        other = ApprovalLedger()
        coordinator = WriteCoordinator(ledger)

        for envelope in (None, ApprovalEnvelope(), approved(other)):
            with self.subTest(envelope=type(envelope).__name__):
                adapter = FakeAdapter()
                result = coordinator.run(envelope, action(), state(), adapter)
                self.assertEqual(result.status, "recovery-required")
                self.assertEqual(adapter.write_count, 0)

    def test_drift_consumes_approval_without_write_and_requires_repreview(self):
        ledger = ApprovalLedger()
        adapter = FakeAdapter(read_value=state("external edit"))
        coordinator = WriteCoordinator(ledger)
        envelope = approved(ledger)

        result = coordinator.run(envelope, action(), state(), adapter)
        retry = coordinator.run(envelope, action(), state(), adapter)

        self.assertEqual(result.reason, "drift")
        self.assertTrue(result.approval_consumed)
        self.assertTrue(result.new_preview_required)
        self.assertEqual(adapter.write_count, 0)
        self.assertEqual(adapter.read_count, 1)
        self.assertEqual(retry.reason, "approval-terminal")
        self.assertEqual(adapter.read_count, 1)

    def test_interruption_before_and_after_read_consume_without_write(self):
        for before, after in ((True, False), (False, True)):
            with self.subTest(before=before, after=after):
                ledger = ApprovalLedger()
                adapter = FakeAdapter()
                result = WriteCoordinator(ledger).run(
                    approved(ledger),
                    action(),
                    state(),
                    adapter,
                    interrupt_before_read=before,
                    interrupt_after_read=after,
                )
                self.assertEqual(result.reason, "interrupted")
                self.assertTrue(result.approval_consumed)
                self.assertEqual(adapter.write_count, 0)
                self.assertEqual(adapter.read_count, 0 if before else 1)

    def test_unusable_reads_fail_closed_and_do_not_leak(self):
        for read_value in (
            ReadFailure("inaccessible"),
            ReadFailure("ambiguous"),
            [],
            {"target": None},
            RuntimeError(CANARY_TASK),
        ):
            with self.subTest(read_value=type(read_value).__name__):
                ledger = ApprovalLedger()
                adapter = FakeAdapter(read_value=read_value)
                result = WriteCoordinator(ledger).run(
                    approved(ledger), action(), state(), adapter
                )
                self.assertEqual(result.status, "recovery-required")
                if isinstance(read_value, dict):
                    self.assertEqual(result.reason, "read-malformed")
                self.assertTrue(result.approval_consumed)
                self.assertEqual(adapter.write_count, 0)
                self.assertNotIn(CANARY_TASK, repr(result))

    def test_changed_noop_and_ambiguous_write_outcomes_never_retry(self):
        for outcome in ("changed", "no-op", "ambiguous", TimeoutError(CANARY_URL)):
            with self.subTest(outcome=repr(outcome)):
                ledger = ApprovalLedger()
                adapter = FakeAdapter(write_value=outcome)
                coordinator = WriteCoordinator(ledger)
                envelope = approved(ledger)
                result = coordinator.run(envelope, action(), state(), adapter)
                retry = coordinator.run(envelope, action(), state(), adapter)
                self.assertEqual(result.status, "recovery-required")
                self.assertEqual(adapter.write_count, 1)
                self.assertEqual(adapter.read_count, 1)
                self.assertEqual(retry.reason, "approval-terminal")

    def test_partial_write_is_explicitly_uncertain_and_not_retried(self):
        ledger = ApprovalLedger()
        adapter = FakeAdapter(write_value="partial")
        coordinator = WriteCoordinator(ledger)
        envelope = approved(ledger)

        result = coordinator.run(envelope, action(), state(), adapter)
        coordinator.run(envelope, action(), state(), adapter)

        self.assertEqual(result.reason, "possible-partial")
        self.assertEqual(result.outcome_certainty, "possible-partial")
        self.assertTrue(result.reinspection_required)
        self.assertTrue(result.new_preview_required)
        self.assertEqual(adapter.write_count, 1)

    def test_concurrent_same_approval_attempts_at_most_one_write(self):
        ledger = ApprovalLedger()
        adapter = FakeAdapter()
        coordinator = WriteCoordinator(ledger)
        envelope = approved(ledger)
        barrier = threading.Barrier(3)
        results = []

        def run():
            barrier.wait()
            results.append(coordinator.run(envelope, action(), state(), adapter))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(adapter.write_count, 1)
        self.assertEqual([result.status for result in results].count("completed"), 1)

    def test_recovery_public_output_is_static_and_has_no_authority(self):
        ledger = ApprovalLedger()
        adapter = FakeAdapter(read_value=ReadFailure("ambiguous"))
        result = WriteCoordinator(ledger).run(
            approved(ledger), action(), state(), adapter
        )
        public = (
            repr(result)
            + repr(result.to_public_dict())
            + repr(result.recovery_preview)
            + json.dumps(dataclasses.asdict(result), sort_keys=True)
        )

        self.assertFalse(result.authorized)
        self.assertIsNotNone(result.recovery_preview)
        for canary in (
            CANARY_ID,
            CANARY_URL,
            CANARY_TASK,
            CANARY_CATEGORY,
            CANARY_STATE,
        ):
            self.assertNotIn(canary, public)


if __name__ == "__main__":
    unittest.main()
