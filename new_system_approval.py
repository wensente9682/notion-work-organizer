"""Pure-local one-action preview and single-use approval for New System."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


_PLACEHOLDERS = {"TODO", "TBD", "PLACEHOLDER", "UNKNOWN", "?"}


class PreviewInputError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"PreviewInputError(code={self.code!r})"


def _is_placeholder(value: str) -> bool:
    return not value.strip() or value.strip().upper() in _PLACEHOLDERS


def _normalize(value: object, error_code: str) -> object:
    if value is None:
        raise PreviewInputError(error_code)
    if isinstance(value, str):
        if _is_placeholder(value):
            raise PreviewInputError(error_code)
        return value
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PreviewInputError(error_code)
        return value
    if isinstance(value, Mapping):
        if not value or not all(isinstance(key, str) and key.strip() for key in value):
            raise PreviewInputError(error_code)
        return {key: _normalize(child, error_code) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        if not value:
            raise PreviewInputError(error_code)
        return [_normalize(child, error_code) for child in value]
    raise PreviewInputError(error_code)


def _canonical_bytes(value: object, error_code: str) -> bytes:
    try:
        return json.dumps(
            _normalize(value, error_code),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except PreviewInputError:
        raise
    except (TypeError, ValueError):
        raise PreviewInputError(error_code) from None


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_action_bytes(action: object) -> bytes:
    """Return the one canonical UTF-8 representation used across write layers."""
    return _canonical_bytes(action, "action.not-canonical")


def canonical_expected_state_bytes(expected_state: object) -> bytes:
    return _canonical_bytes(expected_state, "expected-state.invalid")


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _summary() -> dict[str, object]:
    return {
        "action_count": 1,
        "target_bound": True,
        "payload_bound": True,
    }


def _validate_archive_target_action(
    kind: object, target: object, payload: object
) -> None:
    if kind != "create_archive_target":
        return
    if (
        type(target) is not dict
        or set(target) != {"container", "category"}
        or any(type(value) is not str or _is_placeholder(value) for value in target.values())
        or type(payload) is not dict
        or set(payload) != {"display_name", "schema"}
        or type(payload["display_name"]) is not str
        or _is_placeholder(payload["display_name"])
        or type(payload["schema"]) is not dict
        or not payload["schema"]
    ):
        raise PreviewInputError("action.invalid")
    try:
        display_name_bytes = len(payload["display_name"].encode("utf-8"))
    except UnicodeError:
        raise PreviewInputError("action.invalid") from None
    if display_name_bytes > 128:
        raise PreviewInputError("action.invalid")


class GuidedAction:
    __slots__ = ("_canonical", "_digest")

    def __init__(self, canonical: bytes, digest: str):
        self._canonical = canonical
        self._digest = digest

    @classmethod
    def from_mapping(cls, action: object) -> GuidedAction:
        if not isinstance(action, Mapping) or set(action) != {"kind", "target", "payload"}:
            raise PreviewInputError("action.invalid")
        kind = action["kind"]
        target = action["target"]
        payload = action["payload"]
        if (
            type(kind) is not str
            or _is_placeholder(kind)
            or not isinstance(target, (str, Mapping))
            or not target
            or not isinstance(payload, Mapping)
            or not payload
        ):
            raise PreviewInputError("action.invalid")
        _validate_archive_target_action(kind, target, payload)
        canonical = canonical_action_bytes(action)
        return cls(canonical, _digest(canonical))

    @property
    def digest(self) -> str:
        return self._digest

    def review(self) -> object:
        return _freeze(json.loads(self._canonical))

    def to_public_dict(self) -> dict[str, object]:
        return {"digest": self._digest, **_summary()}

    def __repr__(self) -> str:
        return f"GuidedAction({self.to_public_dict()!r})"


class ActionPreview:
    __slots__ = (
        "_action",
        "_state_digest",
        "_state_canonical",
        "_binding_digest",
        "_trusted",
    )

    def __init__(
        self,
        action: GuidedAction,
        state_canonical: bytes,
        state_digest: str,
        binding_digest: str,
    ):
        self._action = action
        self._state_canonical = state_canonical
        self._state_digest = state_digest
        self._binding_digest = binding_digest
        self._trusted = False

    @property
    def status(self) -> str:
        return "preview-ready"

    @property
    def action_digest(self) -> str:
        return self._action.digest

    @property
    def expected_state_digest(self) -> str:
        return self._state_digest

    @property
    def binding_digest(self) -> str:
        return self._binding_digest

    def review_action(self) -> object:
        return self._action.review()

    def to_public_dict(self) -> dict[str, object]:
        summary = self._action.to_public_dict()
        summary.pop("digest")
        return {
            "status": self.status,
            "action_digest": self.action_digest,
            "expected_state_digest": self.expected_state_digest,
            "binding_digest": self.binding_digest,
            **summary,
        }

    def __repr__(self) -> str:
        return f"ActionPreview({self.to_public_dict()!r})"


def preview_next_action(action: object, expected_state: object) -> ActionPreview:
    guided_action = GuidedAction.from_mapping(action)
    if not isinstance(expected_state, Mapping) or not expected_state:
        raise PreviewInputError("expected-state.invalid")
    state_canonical = canonical_expected_state_bytes(expected_state)
    state_digest = _digest(state_canonical)
    binding = _digest(f"{guided_action.digest}:{state_digest}".encode("ascii"))
    return ActionPreview(guided_action, state_canonical, state_digest, binding)


@dataclass(frozen=True, eq=False)
class ApprovalEnvelope:
    """Opaque, in-memory capability; no bearer material is public."""

    def __copy__(self) -> ApprovalEnvelope:
        return self

    def __deepcopy__(self, memo: object) -> ApprovalEnvelope:
        return self

    def to_public_dict(self) -> dict[str, str]:
        return {"status": "approval-issued"}

    def __repr__(self) -> str:
        return "ApprovalEnvelope(status='approval-issued')"


class AttemptClaim:
    """Opaque, in-memory ownership of one T4 write attempt."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "AttemptClaim()"


@dataclass(frozen=True)
class _ClaimDecision:
    code: str
    claim: AttemptClaim | None
    action_digest: str | None
    attempt_fingerprint: str | None
    action_canonical: bytes | None
    expected_state_canonical: bytes | None


@dataclass
class _ApprovalRecord:
    binding_digest: str
    action_digest: str
    action_canonical: bytes
    expected_state_canonical: bytes
    state: str = "pending"
    claim: AttemptClaim | None = None
    attempt_fingerprint: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    status: str
    code: str
    authorized: bool

    def to_public_dict(self) -> dict[str, object]:
        return {"status": self.status, "code": self.code, "authorized": self.authorized}


class ApprovalLedger:
    def __init__(self) -> None:
        self._previews: dict[ActionPreview, tuple[str, str, str, bytes, bytes, bool]] = {}
        self._approvals: dict[ApprovalEnvelope, _ApprovalRecord] = {}
        self._claims: dict[AttemptClaim, ApprovalEnvelope] = {}
        self._lock = threading.Lock()

    def preview(self, action: object, expected_state: object) -> ActionPreview:
        preview = preview_next_action(action, expected_state)
        with self._lock:
            preview._trusted = True
            self._previews[preview] = (
                preview.binding_digest,
                preview.action_digest,
                preview.expected_state_digest,
                preview._action._canonical,
                preview._state_canonical,
                False,
            )
        return preview

    def issue(self, preview: object, *, accepted: bool) -> ApprovalEnvelope | None:
        if type(preview) is not ActionPreview:
            return None
        with self._lock:
            record = self._previews.get(preview)
            if (
                record is None
                or not preview._trusted
                or record[5]
                or record[0] != preview.binding_digest
                or record[1] != preview.action_digest
                or record[2] != preview.expected_state_digest
                or record[3] != preview._action._canonical
                or record[4] != preview._state_canonical
            ):
                return None
            self._previews[preview] = (*record[:5], True)
            if accepted is not True:
                return None
            envelope = ApprovalEnvelope()
            self._approvals[envelope] = _ApprovalRecord(
                record[0], record[1], record[3], record[4]
            )
            return envelope

    def claim_for_write(self, envelope: object) -> _ClaimDecision:
        if type(envelope) is not ApprovalEnvelope:
            return _ClaimDecision("approval.required", None, None, None, None, None)
        with self._lock:
            record = self._approvals.get(envelope)
            if record is None:
                return _ClaimDecision("approval.unknown", None, None, None, None, None)
            if record.state == "consumed":
                return _ClaimDecision(
                    "approval.consumed",
                    None,
                    record.action_digest,
                    record.attempt_fingerprint,
                    None,
                    None,
                )
            if record.state == "in-progress":
                return _ClaimDecision(
                    "approval.in-progress",
                    None,
                    record.action_digest,
                    record.attempt_fingerprint,
                    None,
                    None,
                )
            claim = AttemptClaim()
            record.state = "in-progress"
            record.claim = claim
            record.attempt_fingerprint = secrets.token_hex(16)
            self._claims[claim] = envelope
            return _ClaimDecision(
                "approval.claimed",
                claim,
                record.action_digest,
                record.attempt_fingerprint,
                record.action_canonical,
                record.expected_state_canonical,
            )

    def _evaluate_consumed(
        self,
        record: _ApprovalRecord,
        action: object,
        expected_state: object,
        *,
        interrupted: bool,
    ) -> AuthorizationDecision:
        if interrupted:
            return AuthorizationDecision("denied", "approval.interrupted", False)
        try:
            attempted = preview_next_action(action, expected_state).binding_digest
        except Exception:
            attempted = None
        if attempted != record.binding_digest:
            return AuthorizationDecision("denied", "approval.binding-mismatch", False)
        return AuthorizationDecision("authorized", "approval.authorized", True)

    def finalize_write_attempt(
        self,
        claim: object,
        action: object,
        expected_state: object,
        *,
        interrupted: bool = False,
    ) -> AuthorizationDecision:
        if type(claim) is not AttemptClaim:
            return AuthorizationDecision("denied", "approval.unknown", False)
        with self._lock:
            envelope = self._claims.get(claim)
            record = self._approvals.get(envelope) if envelope else None
            if record is None:
                return AuthorizationDecision("denied", "approval.unknown", False)
            if record.state == "consumed":
                return AuthorizationDecision("denied", "approval.consumed", False)
            if record.state != "in-progress" or record.claim is not claim:
                return AuthorizationDecision("denied", "approval.in-progress", False)
            record.state = "consumed"
        return self._evaluate_consumed(
            record,
            action,
            expected_state,
            interrupted=interrupted,
        )

    def consume(
        self,
        envelope: object,
        action: object,
        expected_state: object,
        *,
        interrupted: bool = False,
    ) -> AuthorizationDecision:
        if type(envelope) is not ApprovalEnvelope:
            return AuthorizationDecision("denied", "approval.required", False)
        with self._lock:
            record = self._approvals.get(envelope)
            if record is None:
                return AuthorizationDecision("denied", "approval.unknown", False)
            if record.state == "consumed":
                return AuthorizationDecision("denied", "approval.consumed", False)
            if record.state == "in-progress":
                return AuthorizationDecision("denied", "approval.in-progress", False)
            record.state = "consumed"
        return self._evaluate_consumed(
            record,
            action,
            expected_state,
            interrupted=interrupted,
        )

    def __repr__(self) -> str:
        return "ApprovalLedger()"
