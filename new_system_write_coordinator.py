"""Fail-closed local write coordinator for one approved New System action."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from new_system_approval import (
    ApprovalLedger,
    PreviewInputError,
    canonical_action_bytes,
    canonical_expected_state_bytes,
    preview_next_action,
)
from new_system_recovery_journal import RecoveryJournal


@dataclass(frozen=True)
class ReadFailure:
    reason: object


@dataclass(frozen=True)
class RecoveryPreview:
    status: str
    reason: str
    action_digest: str | None
    attempt_fingerprint: str | None
    phase: str
    reinspection_required: bool
    new_preview_required: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "action_digest": self.action_digest,
            "attempt_fingerprint": self.attempt_fingerprint,
            "phase": self.phase,
            "reinspection_required": self.reinspection_required,
            "new_preview_required": self.new_preview_required,
        }



@dataclass(frozen=True)
class WriteResult:
    status: str
    reason: str
    authorized: bool
    approval_consumed: bool
    read_attempted: bool
    write_attempted: bool
    outcome_certainty: str
    reinspection_required: bool
    new_preview_required: bool
    action_digest: str | None
    attempt_fingerprint: str | None
    phase: str
    recovery_preview: RecoveryPreview | None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "authorized": self.authorized,
            "approval_consumed": self.approval_consumed,
            "read_attempted": self.read_attempted,
            "write_attempted": self.write_attempted,
            "outcome_certainty": self.outcome_certainty,
            "reinspection_required": self.reinspection_required,
            "new_preview_required": self.new_preview_required,
            "action_digest": self.action_digest,
            "attempt_fingerprint": self.attempt_fingerprint,
            "phase": self.phase,
            "recovery_preview": (
                self.recovery_preview.to_public_dict()
                if self.recovery_preview
                else None
            ),
        }



class WriteCoordinator:
    def __init__(self, ledger: ApprovalLedger):
        self._ledger = ledger

    def _recovery(
        self,
        reason: str,
        *,
        approval_consumed: bool,
        read_attempted: bool,
        write_attempted: bool,
        outcome_certainty: str,
        action_digest: str | None,
        attempt_fingerprint: str | None,
        phase: str = "recovery",
    ) -> WriteResult:
        preview = RecoveryPreview(
            "recovery-review-required",
            reason,
            action_digest,
            attempt_fingerprint,
            phase,
            True,
            True,
        )
        return WriteResult(
            "recovery-required",
            reason,
            False,
            approval_consumed,
            read_attempted,
            write_attempted,
            outcome_certainty,
            True,
            True,
            action_digest,
            attempt_fingerprint,
            phase,
            preview,
        )

    def _claim_result(self, decision: object) -> WriteResult:
        code = decision.code
        if code == "approval.in-progress":
            return WriteResult(
                "attempt-in-progress",
                "attempt-in-progress",
                False,
                False,
                False,
                False,
                "not-attempted",
                False,
                False,
                decision.action_digest,
                decision.attempt_fingerprint,
                "claim",
                None,
            )
        if code == "approval.consumed":
            return self._recovery(
                "approval-terminal",
                approval_consumed=True,
                read_attempted=False,
                write_attempted=False,
                outcome_certainty="not-attempted",
                action_digest=decision.action_digest,
                attempt_fingerprint=decision.attempt_fingerprint,
                phase="claim",
            )
        return self._recovery(
            "approval-required",
            approval_consumed=False,
            read_attempted=False,
            write_attempted=False,
            outcome_certainty="not-attempted",
            action_digest=None,
            attempt_fingerprint=None,
            phase="claim",
        )

    def run(
        self,
        envelope: object,
        action: object,
        expected_state: object,
        adapter: object,
        *,
        interrupt_before_read: bool = False,
        interrupt_after_read: bool = False,
        recovery_journal: object | None = None,
    ) -> WriteResult:
        if recovery_journal is not None:
            return self._run_journal(
                envelope, action, expected_state, adapter, recovery_journal,
                interrupt_before_read=interrupt_before_read,
                interrupt_after_read=interrupt_after_read,
            )
        claimed = self._ledger.claim_for_write(envelope)
        if claimed.claim is None:
            return self._claim_result(claimed)
        claim = claimed.claim
        action_digest = claimed.action_digest
        fingerprint = claimed.attempt_fingerprint
        action_canonical = claimed.action_canonical
        expected_state_canonical = claimed.expected_state_canonical
        phase = "claim"
        read_attempted = False
        write_attempted = False

        def recovery(
            reason: str,
            *,
            interrupted: bool = False,
        ) -> WriteResult:
            try:
                decision = self._ledger.finalize_write_attempt(
                    claim,
                    action,
                    expected_state if interrupted else {},
                    interrupted=interrupted,
                )
                consumed = decision.code != "approval.unknown"
            except Exception:
                consumed = False
            return self._recovery(
                reason,
                approval_consumed=consumed,
                read_attempted=read_attempted,
                write_attempted=write_attempted,
                outcome_certainty="not-attempted",
                action_digest=action_digest,
                attempt_fingerprint=fingerprint,
                phase=phase,
            )

        try:
            if (
                type(action_canonical) is not bytes
                or type(expected_state_canonical) is not bytes
                or not self._matches_claimed_snapshot(
                    action,
                    expected_state,
                    action_canonical,
                    expected_state_canonical,
                )
            ):
                return recovery("approval-required", interrupted=True)
            if interrupt_before_read:
                return recovery(
                    "interrupted",
                    interrupted=True,
                )
            phase = "read"
            read_attempted = True
            try:
                observed = adapter.read_exact(self._decode_snapshot(action_canonical))
            except Exception:
                return recovery("read-unavailable")
            if not self._matches_claimed_snapshot(
                action,
                expected_state,
                action_canonical,
                expected_state_canonical,
            ):
                return recovery("authority-drift", interrupted=True)
            if isinstance(observed, ReadFailure):
                if type(observed.reason) is str:
                    if observed.reason == "inaccessible":
                        reason = "read-inaccessible"
                    elif observed.reason == "ambiguous":
                        reason = "read-ambiguous"
                    else:
                        reason = "read-unusable"
                else:
                    reason = "read-unusable"
                return recovery(reason)
            if not isinstance(observed, Mapping) or not observed:
                return recovery("read-malformed")
            try:
                preview_next_action(self._decode_snapshot(action_canonical), observed)
            except Exception:
                return recovery("read-malformed")
            if interrupt_after_read:
                return recovery(
                    "interrupted",
                    interrupted=True,
                )
            phase = "authorize"
            decision = self._ledger.finalize_write_attempt(
                claim, self._decode_snapshot(action_canonical), observed
            )
            if not decision.authorized:
                return self._recovery(
                    "drift" if decision.code == "approval.binding-mismatch" else "approval-denied",
                    approval_consumed=True,
                    read_attempted=read_attempted,
                    write_attempted=write_attempted,
                    outcome_certainty="not-attempted",
                    action_digest=action_digest,
                    attempt_fingerprint=fingerprint,
                    phase=phase,
                )
            phase = "write"
            write_attempted = True
            try:
                if not self._matches_claimed_snapshot(
                    action,
                    expected_state,
                    action_canonical,
                    expected_state_canonical,
                ):
                    return recovery("authority-drift", interrupted=True)
                outcome = adapter.write_once(self._decode_snapshot(action_canonical))
            except Exception:
                outcome = None
            if type(outcome) is not str:
                outcome = "unknown"
            if outcome == "success":
                phase = "completed"
                return WriteResult(
                    "completed",
                    "completed",
                    True,
                    True,
                    read_attempted,
                    write_attempted,
                    "confirmed",
                    False,
                    False,
                    action_digest,
                    fingerprint,
                    phase,
                    None,
                )
            if outcome == "changed" or outcome == "no-op":
                return self._recovery(
                    "write-not-applied",
                    approval_consumed=True,
                    read_attempted=read_attempted,
                    write_attempted=write_attempted,
                    outcome_certainty="not-applied",
                    action_digest=action_digest,
                    attempt_fingerprint=fingerprint,
                    phase=phase,
                )
            if outcome == "partial":
                return self._recovery(
                    "possible-partial",
                    approval_consumed=True,
                    read_attempted=read_attempted,
                    write_attempted=write_attempted,
                    outcome_certainty="possible-partial",
                    action_digest=action_digest,
                    attempt_fingerprint=fingerprint,
                    phase=phase,
                )
            return self._recovery(
                "outcome-unknown",
                approval_consumed=True,
                read_attempted=read_attempted,
                write_attempted=write_attempted,
                outcome_certainty="unknown",
                action_digest=action_digest,
                attempt_fingerprint=fingerprint,
                phase=phase,
            )
        except Exception:
            return recovery("internal-failure", interrupted=True)

    @staticmethod
    def _decode_snapshot(canonical: bytes) -> object:
        return json.loads(canonical)

    @staticmethod
    def _matches_claimed_snapshot(
        action: object,
        expected_state: object,
        action_canonical: bytes,
        expected_state_canonical: bytes,
    ) -> bool:
        try:
            return (
                canonical_action_bytes(action) == action_canonical
                and canonical_expected_state_bytes(expected_state)
                == expected_state_canonical
            )
        except Exception:
            return False

    def _run_journal(
        self, envelope: object, action: object, expected_state: object, adapter: object,
        journal: object, *, interrupt_before_read: bool, interrupt_after_read: bool,
    ) -> WriteResult:
        def result(reason: str, consumed: bool = False, read: bool = False, write: bool = False, phase: str = "recovery", certainty: str = "not-attempted") -> WriteResult:
            return self._recovery(reason, approval_consumed=consumed, read_attempted=read,
                                  write_attempted=write, outcome_certainty=certainty,
                                  action_digest=None, attempt_fingerprint=None, phase=phase)
        if type(journal) is not RecoveryJournal:
            return result("journal-required")
        try:
            from new_system_connector_action_adapter import CanonicalConnectorActionAdapter
        except Exception:
            return result("adapter-required")
        if type(adapter) is not CanonicalConnectorActionAdapter:
            return result("adapter-required")
        try:
            existing = journal.resume_review().to_public_dict()
        except Exception:
            return result("journal-unavailable")
        if type(existing) is not dict or existing.get("status") != "fresh-review-required" or existing.get("phase") is not None or existing.get("failure_class") != "none":
            return result("journal-review-required")
        claimed = self._ledger.claim_for_write(envelope)
        if claimed.claim is None:
            return result("approval-required", claimed.code == "approval.consumed")
        claim, digest, fingerprint = claimed.claim, claimed.action_digest, claimed.attempt_fingerprint
        action_canonical = claimed.action_canonical
        expected_state_canonical = claimed.expected_state_canonical
        if (
            type(digest) is not str
            or type(fingerprint) is not str
            or type(action_canonical) is not bytes
            or type(expected_state_canonical) is not bytes
            or not self._matches_claimed_snapshot(
                action,
                expected_state,
                action_canonical,
                expected_state_canonical,
            )
        ):
            self._ledger.finalize_write_attempt(claim, action, expected_state, interrupted=True)
            return result("approval-required", True)
        def fact(phase: str) -> dict[str, object]:
            writing = phase in {"write-started", "confirmed-applied", "confirmed-not-applied", "outcome-unknown", "possible-partial"}
            return {"action_digest": digest, "attempt_fingerprint": fingerprint,
                    "read_attempted": phase != "prepared", "write_attempted": writing,
                    "write_started": writing}
        def advance(phase: str) -> bool:
            try:
                public = journal.transition(phase, fact(phase)).to_public_dict()
                return type(public) is dict and public.get("status") == "accepted"
            except Exception:
                return False
        def stop(reason: str, *, read: bool = False, write: bool = False, phase: str = "recovery", certainty: str = "not-attempted", interrupted: bool = True) -> WriteResult:
            if interrupted:
                try:
                    journal.transition("interrupted", {"action_digest": digest, "attempt_fingerprint": fingerprint,
                                                         "read_attempted": read, "write_attempted": write,
                                                         "write_started": write}).to_public_dict()
                except Exception:
                    pass
            try:
                self._ledger.finalize_write_attempt(claim, action, expected_state, interrupted=True)
            except Exception:
                pass
            return result(reason, True, read, write, phase, certainty)
        if not advance("prepared"):
            return stop("journal-failed")
        if not self._matches_claimed_snapshot(action, expected_state, action_canonical, expected_state_canonical):
            return stop("authority-drift")
        if interrupt_before_read:
            return stop("interrupted", phase="claim")
        try:
            observed = adapter.read_exact(self._decode_snapshot(action_canonical))
        except Exception:
            return stop("read-unavailable", read=True, phase="read")
        if not self._matches_claimed_snapshot(action, expected_state, action_canonical, expected_state_canonical):
            return stop("authority-drift", read=True, phase="read")
        if isinstance(observed, ReadFailure) or not isinstance(observed, Mapping) or not observed:
            return stop("read-unavailable", read=True, phase="read")
        try:
            preview_next_action(self._decode_snapshot(action_canonical), observed)
        except Exception:
            return stop("read-unavailable", read=True, phase="read")
        if not advance("read-completed"):
            return stop("journal-failed", read=True, phase="read")
        if not self._matches_claimed_snapshot(action, expected_state, action_canonical, expected_state_canonical):
            return stop("authority-drift", read=True, phase="read")
        if interrupt_after_read:
            return stop("interrupted", read=True, phase="read")
        approved_expected_state = self._decode_snapshot(expected_state_canonical)
        if observed != approved_expected_state:
            return stop("drift", read=True, phase="authorize")
        decision = self._ledger.finalize_write_attempt(
            claim, self._decode_snapshot(action_canonical), observed
        )
        if not decision.authorized:
            return stop("drift", read=True, phase="authorize", interrupted=False)
        if not advance("write-ready"):
            return result("journal-failed", True, True, False, "write")
        if not self._matches_claimed_snapshot(action, expected_state, action_canonical, expected_state_canonical):
            return stop("authority-drift", read=True, phase="authorize")
        try:
            evidence = CanonicalConnectorActionAdapter._write_once_evidenced(
                adapter, self._decode_snapshot(action_canonical)
            )
        except Exception:
            evidence = None
        try:
            from new_system_connector_action_adapter import _AdapterWrite
            attempted = type(evidence) is _AdapterWrite and evidence._attempted is True
            outcome = evidence._outcome if type(evidence) is _AdapterWrite else "unknown"
        except Exception:
            attempted, outcome = False, "unknown"
        if not attempted:
            return stop("write-not-attempted", read=True, phase="authorize")
        if not advance("write-started"):
            return result("journal-failed", True, True, True, "write", "unknown")
        if type(outcome) is not str:
            outcome = "unknown"
        terminal = {"success": ("confirmed-applied", "completed", "confirmed"),
                    "changed": ("confirmed-not-applied", "write-not-applied", "not-applied"),
                    "no-op": ("confirmed-not-applied", "write-not-applied", "not-applied"),
                    "partial": ("possible-partial", "possible-partial", "possible-partial")}.get(outcome, ("outcome-unknown", "outcome-unknown", "unknown"))
        if not advance(terminal[0]):
            return result("journal-failed", True, True, True, "write", terminal[2])
        if terminal[1] != "completed":
            return result(terminal[1], True, True, True, "write", terminal[2])
        return WriteResult("completed", "completed", True, True, True, True, "confirmed", False, False, None, None, "completed", None)
