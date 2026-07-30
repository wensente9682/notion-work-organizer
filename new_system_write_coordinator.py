"""Fail-closed local write coordinator for one approved New System action."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from new_system_approval import ApprovalLedger, PreviewInputError, preview_next_action


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
    ) -> WriteResult:
        claimed = self._ledger.claim_for_write(envelope)
        if claimed.claim is None:
            return self._claim_result(claimed)
        claim = claimed.claim
        action_digest = claimed.action_digest
        fingerprint = claimed.attempt_fingerprint
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
            if interrupt_before_read:
                return recovery(
                    "interrupted",
                    interrupted=True,
                )
            phase = "read"
            read_attempted = True
            try:
                observed = adapter.read_exact(action)
            except Exception:
                return recovery("read-unavailable")
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
                preview_next_action(action, observed)
            except Exception:
                return recovery("read-malformed")
            if interrupt_after_read:
                return recovery(
                    "interrupted",
                    interrupted=True,
                )
            phase = "authorize"
            decision = self._ledger.finalize_write_attempt(claim, action, observed)
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
                outcome = adapter.write_once(action)
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
