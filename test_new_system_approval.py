import copy
import dataclasses
import json
import math
import threading
import unittest
from collections.abc import Mapping

from new_system_approval import (
    ApprovalEnvelope,
    ApprovalLedger,
    AttemptClaim,
    ActionPreview,
    GuidedAction,
    PreviewInputError,
    preview_next_action,
)


CANARY_ID = "db-secret-123"
CANARY_URL = "https://notion.so/secret-page"
CANARY_TASK = "private task text"
CANARY_CATEGORY = "Private Category"
CANARY_STATE = "private expected state"


def action(**overrides):
    values = {
        "kind": "create_database",
        "target": {"parent_id": CANARY_ID},
        "payload": {
            "title": CANARY_TASK,
            "category": CANARY_CATEGORY,
            "url": CANARY_URL,
        },
    }
    values.update(overrides)
    return values


def expected_state():
    return {"parent": {"id": CANARY_ID}, "state": CANARY_STATE}


class NewSystemApprovalTests(unittest.TestCase):
    def test_opaque_subclasses_are_rejected_before_protocol_calls(self):
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

        class EvilClaim(AttemptClaim):
            def __hash__(self):
                calls.append("claim-hash")
                raise RuntimeError(CANARY_TASK)

        class EvilPreview(ActionPreview):
            def __hash__(self):
                calls.append("preview-hash")
                raise RuntimeError(CANARY_TASK)

        ledger = ApprovalLedger()
        envelope = EvilEnvelope()
        preview = EvilPreview.__new__(EvilPreview)

        self.assertIsNone(ledger.issue(preview, accepted=True))
        self.assertEqual(ledger.claim_for_write(envelope).code, "approval.required")
        self.assertEqual(
            ledger.consume(envelope, action(), expected_state()).code,
            "approval.required",
        )
        self.assertEqual(
            ledger.finalize_write_attempt(
                EvilClaim(), action(), expected_state()
            ).code,
            "approval.unknown",
        )
        self.assertEqual(calls, [])
    def test_direct_canonicalization_exceptions_are_denied_and_consumed(self):
        class ExplodingMapping(Mapping):
            def __getitem__(self, _key):
                raise RuntimeError(CANARY_TASK)

            def __iter__(self):
                raise RuntimeError(CANARY_TASK)

            def __len__(self):
                return 1

        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(action(), expected_state()), accepted=True
        )
        direct = ledger.consume(envelope, ExplodingMapping(), expected_state())
        retry = ledger.consume(envelope, action(), expected_state())

        other = ApprovalLedger()
        other_envelope = other.issue(
            other.preview(action(), expected_state()), accepted=True
        )
        claim = other.claim_for_write(other_envelope)
        finalized = other.finalize_write_attempt(
            claim.claim, action(), ExplodingMapping()
        )

        self.assertEqual(direct.code, "approval.binding-mismatch")
        self.assertEqual(retry.code, "approval.consumed")
        self.assertEqual(finalized.code, "approval.binding-mismatch")
        self.assertNotIn(CANARY_TASK, repr(direct) + repr(finalized))
    def test_claim_blocks_direct_consume_until_finalize(self):
        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(action(), expected_state()), accepted=True
        )

        claim = ledger.claim_for_write(envelope)
        blocked = ledger.consume(envelope, action(), expected_state())
        finalized = ledger.finalize_write_attempt(
            claim.claim, action(), expected_state()
        )

        self.assertEqual(claim.code, "approval.claimed")
        self.assertEqual(blocked.code, "approval.in-progress")
        self.assertTrue(finalized.authorized)
    def test_public_summary_rejects_kind_and_key_canary_injection(self):
        kind_canary = "kind-db-secret-123"
        target_key_canary = "target-https://notion.so/secret-page"
        payload_key_canary = "payload-private-task-text"
        injected = action(
            kind=kind_canary,
            target={target_key_canary: "bound"},
            payload={payload_key_canary: "bound"},
        )
        guided = GuidedAction.from_mapping(injected)
        preview = ApprovalLedger().preview(injected, expected_state())
        public_values = (
            repr(guided),
            repr(guided.to_public_dict()),
            repr(preview),
            repr(preview.to_public_dict()),
            json.dumps(guided.to_public_dict(), sort_keys=True),
            json.dumps(preview.to_public_dict(), sort_keys=True),
        )

        combined = "\n".join(public_values)
        for canary in (kind_canary, target_key_canary, payload_key_canary):
            self.assertNotIn(canary, combined)

    def test_trusted_preview_exposes_private_review_and_safe_public_summary(self):
        ledger = ApprovalLedger()
        mutable_action = action()
        mutable_state = expected_state()

        preview = ledger.preview(mutable_action, mutable_state)
        review = preview.review_action()
        public = preview.to_public_dict()
        mutable_action["payload"]["title"] = "mutated"
        mutable_state["state"] = "mutated"

        self.assertEqual(review["kind"], "create_database")
        self.assertEqual(review["target"]["parent_id"], CANARY_ID)
        self.assertEqual(review["payload"]["title"], CANARY_TASK)
        with self.assertRaises(TypeError):
            review["payload"]["title"] = "changed"
        self.assertEqual(public["action_count"], 1)
        self.assertTrue(public["target_bound"])
        self.assertTrue(public["payload_bound"])
        self.assertNotIn(CANARY_ID, repr(public))
        self.assertNotIn(CANARY_TASK, repr(public))

    def test_only_current_ledger_can_issue_one_resolution_for_trusted_preview(self):
        ledger = ApprovalLedger()
        other_ledger = ApprovalLedger()
        preview = ledger.preview(action(), expected_state())
        forged = preview_next_action(action(), expected_state())

        self.assertIsNone(ledger.issue(forged, accepted=True))
        self.assertIsNone(other_ledger.issue(preview, accepted=True))
        self.assertIsNotNone(ledger.issue(preview, accepted=True))
        self.assertIsNone(ledger.issue(preview, accepted=True))

    def test_tampered_trusted_preview_cannot_issue(self):
        ledger = ApprovalLedger()
        preview = ledger.preview(action(), expected_state())

        object.__setattr__(preview, "_binding_digest", "tampered")

        self.assertIsNone(ledger.issue(preview, accepted=True))

    def test_rejected_trusted_preview_cannot_be_issued_later(self):
        ledger = ApprovalLedger()
        preview = ledger.preview(action(), expected_state())

        self.assertIsNone(ledger.issue(preview, accepted=False))
        self.assertIsNone(ledger.issue(preview, accepted=True))

    def test_complete_input_rejects_missing_and_placeholder_values(self):
        ledger = ApprovalLedger()
        invalid_states = (
            None,
            {},
            [],
            "",
            0,
            False,
            {"state": None},
            {"state": " UNKNOWN "},
        )
        invalid_actions = (
            action(kind=" TODO "),
            action(target={"parent_id": None}),
            action(target={"parent_id": "  "}),
            action(payload={"title": "TBD"}),
            action(payload={"title": {"nested": "?"}}),
        )

        for state in invalid_states:
            with self.subTest(state=repr(state)):
                with self.assertRaises(PreviewInputError):
                    ledger.preview(action(), state)
        for candidate in invalid_actions:
            with self.subTest(candidate=repr(candidate)):
                with self.assertRaises(PreviewInputError):
                    ledger.preview(candidate, expected_state())

    def test_capability_and_malformed_inputs_are_private_and_non_throwing(self):
        ledger = ApprovalLedger()
        preview = ledger.preview(action(), expected_state())
        envelope = ledger.issue(preview, accepted=True)
        public = repr(envelope) + repr(dataclasses.asdict(envelope))

        self.assertEqual(dataclasses.asdict(envelope), {})
        self.assertNotIn(CANARY_ID, public)
        self.assertEqual(
            ledger.consume("bad", action(), expected_state()).status,
            "denied",
        )
        self.assertEqual(
            ledger.consume(envelope, action(), None).status,
            "denied",
        )

    def test_one_action_preview_happy_path(self):
        preview = preview_next_action(action(), expected_state())

        self.assertEqual(preview.status, "preview-ready")
        self.assertEqual(len(preview.action_digest), 64)
        self.assertEqual(len(preview.expected_state_digest), 64)
        self.assertEqual(len(preview.binding_digest), 64)

    def test_bulk_and_incomplete_actions_are_rejected(self):
        invalid = (
            [action(), action()],
            {"kind": "create_database", "payload": {"title": "x"}},
            action(target=""),
            action(payload={}),
            action(kind=""),
        )

        for candidate in invalid:
            with self.subTest(candidate=type(candidate).__name__):
                with self.assertRaises(PreviewInputError) as caught:
                    preview_next_action(candidate, expected_state())
                self.assertEqual(caught.exception.code, "action.invalid")

    def test_digest_is_deterministic_across_mapping_order(self):
        first = action(
            target={"parent_id": CANARY_ID, "kind": "page"},
            payload={"title": CANARY_TASK, "category": CANARY_CATEGORY},
        )
        second = {
            "payload": {"category": CANARY_CATEGORY, "title": CANARY_TASK},
            "target": {"kind": "page", "parent_id": CANARY_ID},
            "kind": "create_database",
        }
        state_a = {"version": 1, "parent": {"id": CANARY_ID, "type": "page"}}
        state_b = {"parent": {"type": "page", "id": CANARY_ID}, "version": 1}

        self.assertEqual(
            preview_next_action(first, state_a).binding_digest,
            preview_next_action(second, state_b).binding_digest,
        )

    def test_preview_is_safe_from_external_mutation(self):
        mutable_action = action()
        mutable_state = expected_state()
        preview = preview_next_action(mutable_action, mutable_state)

        mutable_action["payload"]["title"] = "mutated"
        mutable_state["state"] = "mutated"

        self.assertEqual(
            preview.binding_digest,
            preview_next_action(action(), expected_state()).binding_digest,
        )

    def test_unserializable_or_nondeterministic_input_fails_closed(self):
        invalid_values = (
            action(payload={"callback": lambda: None}),
            action(payload={"bad": {1, 2}}),
            action(payload={"bad": math.nan}),
            action(payload={1: "non-string key"}),
        )

        for candidate in invalid_values:
            with self.subTest(value=type(candidate["payload"]).__name__):
                with self.assertRaises(PreviewInputError) as caught:
                    preview_next_action(candidate, expected_state())
                self.assertEqual(caught.exception.code, "action.not-canonical")

    def test_exact_action_first_consume_authorizes_once(self):
        ledger = ApprovalLedger()
        preview = ledger.preview(action(), expected_state())
        envelope = ledger.issue(preview, accepted=True)

        decision = ledger.consume(envelope, action(), expected_state())

        self.assertEqual(decision.status, "authorized")
        self.assertEqual(decision.code, "approval.authorized")
        self.assertTrue(decision.authorized)

    def test_wrong_action_denies_and_consumes(self):
        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(action(), expected_state()),
            accepted=True,
        )

        first = ledger.consume(
            envelope,
            action(payload={"title": "different"}),
            expected_state(),
        )
        second = ledger.consume(envelope, action(), expected_state())

        self.assertEqual(first.code, "approval.binding-mismatch")
        self.assertFalse(first.authorized)
        self.assertEqual(second.code, "approval.consumed")

    def test_wrong_expected_state_denies_and_consumes(self):
        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(action(), expected_state()),
            accepted=True,
        )

        first = ledger.consume(envelope, action(), {"state": "different"})
        second = ledger.consume(envelope, action(), expected_state())

        self.assertEqual(first.code, "approval.binding-mismatch")
        self.assertEqual(second.code, "approval.consumed")

    def test_copy_and_duplicate_consume_have_no_second_authority(self):
        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(action(), expected_state()),
            accepted=True,
        )
        copied = copy.copy(envelope)

        self.assertTrue(
            ledger.consume(envelope, action(), expected_state()).authorized
        )
        self.assertEqual(
            ledger.consume(copied, action(), expected_state()).code,
            "approval.consumed",
        )

    def test_unknown_and_forged_envelopes_are_rejected(self):
        ledger = ApprovalLedger()
        forged = ApprovalEnvelope()

        self.assertEqual(
            ledger.consume(forged, action(), expected_state()).code,
            "approval.unknown",
        )
        self.assertEqual(
            ledger.consume(None, action(), expected_state()).code,
            "approval.required",
        )

    def test_interruption_consumes_approval(self):
        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(action(), expected_state()),
            accepted=True,
        )

        interrupted = ledger.consume(
            envelope,
            action(),
            expected_state(),
            interrupted=True,
        )
        retry = ledger.consume(envelope, action(), expected_state())

        self.assertEqual(interrupted.code, "approval.interrupted")
        self.assertEqual(retry.code, "approval.consumed")

    def test_rejection_creates_no_approval_or_authority(self):
        ledger = ApprovalLedger()
        preview = ledger.preview(action(), expected_state())

        envelope = ledger.issue(preview, accepted=False)

        self.assertIsNone(envelope)
        self.assertEqual(
            ledger.consume(envelope, action(), expected_state()).code,
            "approval.required",
        )

    def test_envelope_and_ledger_have_no_ttl_or_time_fields(self):
        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(action(), expected_state()),
            accepted=True,
        )
        field_names = {field.name for field in dataclasses.fields(envelope)}
        public = repr(envelope) + repr(ledger)

        self.assertFalse(
            field_names
            & {"ttl", "expires_at", "expires", "issued_at", "created_at"}
        )
        self.assertNotIn("ttl", public.lower())
        self.assertNotIn("expire", public.lower())

    def test_racing_double_consume_authorizes_at_most_once(self):
        ledger = ApprovalLedger()
        envelope = ledger.issue(
            ledger.preview(action(), expected_state()),
            accepted=True,
        )
        barrier = threading.Barrier(3)
        decisions = []

        def consume():
            barrier.wait()
            decisions.append(
                ledger.consume(envelope, action(), expected_state())
            )

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(
            [decision.authorized for decision in decisions].count(True),
            1,
        )
        self.assertEqual(
            {decision.code for decision in decisions},
            {"approval.authorized", "approval.consumed"},
        )

    def test_public_outputs_and_errors_are_sanitized(self):
        ledger = ApprovalLedger()
        preview = ledger.preview(action(), expected_state())
        envelope = ledger.issue(preview, accepted=True)
        decision = ledger.consume(
            envelope,
            action(payload={"title": "different"}),
            expected_state(),
        )
        with self.assertRaises(PreviewInputError) as caught:
            preview_next_action(action(payload={"bad": object()}), expected_state())

        public_values = (
            repr(GuidedAction.from_mapping(action())),
            repr(GuidedAction.from_mapping(action()).to_public_dict()),
            repr(preview),
            repr(envelope),
            repr(ledger),
            repr(decision),
            repr(caught.exception),
            json.dumps(preview.to_public_dict(), sort_keys=True),
            json.dumps(dataclasses.asdict(envelope), sort_keys=True),
            json.dumps(dataclasses.asdict(decision), sort_keys=True),
        )
        combined = "\n".join(public_values)
        for canary in (
            CANARY_ID,
            CANARY_URL,
            CANARY_TASK,
            CANARY_CATEGORY,
            CANARY_STATE,
        ):
            self.assertNotIn(canary, combined)


if __name__ == "__main__":
    unittest.main()
